"""local-gateway HTTP behaviour: JWT validation, policy, routing, header changes, pass-through and the NOT-XC label.

Every pool here is an echo app, so each test sees exactly what the gateway forwarded.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from copperline_common.outbound import HttpResult
from copperline_common.xcblock import ORIGIN_HEADER, classify
from cryptography.hazmat.primitives.asymmetric import rsa
from local_gateway.app import HEALTH_PATH

TOOL_CLAIMS = {"sub": "shopper", "agent_id": "shopper", "agent_role": "shopper", "obo_sub": "alex.rivera",
               "obo_role": "customer", "aud": "copperline-tools"}
ORDER_STATUS = {"Mcp-Method": "tools/call", "Mcp-Name": "get_order_status"}


def run(coro):
    return asyncio.run(coro)


async def post(client, path, *, token=None, headers=None, **kwargs):
    """headers: a dict, or a list of (name, value) pairs to send repeats or raw bytes exactly as given."""
    if isinstance(headers, list):
        headers = list(headers) + ([("authorization", f"Bearer {token}")] if token else [])
    else:
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
    async with client:
        return await client.post(path, headers=headers, **kwargs)


def raw_headers(pairs) -> list[tuple[bytes, bytes]]:
    """Byte pairs reach the gateway unchanged: repeats stay separate lines and non-ASCII bytes stay bytes."""
    return [(name.encode(), value if isinstance(value, bytes) else value.encode()) for name, value in pairs]


def assert_local_gateway_denial(response, status):
    assert response.status_code == status
    assert ORIGIN_HEADER not in response.headers
    assert response.headers["x-local-gateway"]
    assert "NOT XC" in response.json()["note"]
    result = HttpResult(status=status, headers=dict(response.headers), body_text=response.text,
                        json=response.json(), latency_ms=1)
    blocked_by, reason = classify(result, via_lb=True)
    assert blocked_by == "xc" and reason.startswith("local-gateway (NOT XC)")


def test_allowed_call_is_forwarded_with_its_headers_and_body(echo_gateway, token):
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", token=token(TOOL_CLAIMS),
                        headers={**ORDER_STATUS, "x-request-id": "rid-1"}, content=b'{"x": 1}'))
    assert response.status_code == 200
    echoed = response.json()
    sent = dict(echoed["headers"])
    assert (echoed["path"], echoed["body"]) == ("/mcp/orders", '{"x": 1}')
    assert sent["mcp-name"] == "get_order_status" and sent["x-request-id"] == "rid-1"
    assert sent["authorization"].startswith("Bearer ")
    assert response.headers[ORIGIN_HEADER] == "echo" and response.headers["x-echo-extra"] == "kept"
    assert "x-local-gateway" not in response.headers


def test_upstream_errors_pass_through_as_server_errors(echo_gateway, token):
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", token=token(TOOL_CLAIMS),
                        headers={**ORDER_STATUS, "x-echo-status": "400"}))
    assert response.status_code == 400 and response.headers[ORIGIN_HEADER] == "echo"


def test_a_missing_request_id_is_generated_and_forwarded(echo_gateway, token):
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", token=token(TOOL_CLAIMS), headers=ORDER_STATUS))
    assert dict(response.json()["headers"])["x-request-id"]


@pytest.mark.parametrize(
    "case, claims, ttl, other_key",
    [
        ("no token", None, 300, False),
        ("wrong audience", {**TOOL_CLAIMS, "aud": "copperline-shop"}, 300, False),
        ("expired", TOOL_CLAIMS, -10, False),
        ("signed by another key", TOOL_CLAIMS, 300, True),
        ("missing mandatory obo_role", {k: v for k, v in TOOL_CLAIMS.items() if k != "obo_role"}, 300, False),
        ("wrong issuer", {**TOOL_CLAIMS, "iss": "https://evil.test"}, 300, False),  # sign() keeps a given iss
    ],
)
def test_jwt_failures_are_401_from_the_gateway(echo_gateway, token, case, claims, ttl, other_key):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if other_key else None
    bearer = token(claims, ttl_s=ttl, key=key) if claims else None
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", token=bearer, headers=ORDER_STATUS))
    assert_local_gateway_denial(response, 401)
    assert response.headers["www-authenticate"] == "Bearer"


def test_jwt_is_checked_before_policy(echo_gateway):
    response = run(post(echo_gateway("tools-lb"), "/mcp/refunds", headers={"Mcp-Method": "tools/call"}))
    assert response.status_code == 401


def test_policy_denial_is_403_and_never_reaches_the_pool(echo_gateway, token):
    response = run(post(echo_gateway("tools-lb"), "/mcp/refunds", token=token(TOOL_CLAIMS),
                        headers={"Mcp-Method": "tools/call", "Mcp-Name": "issue_refund"}))
    assert_local_gateway_denial(response, 403)
    assert "default-deny" in response.json()["reason"]


def test_jwt_only_on_target_paths(echo_gateway):
    """shop-lb validates JWTs on /v1/chat only; the login route is public."""
    login = run(post(echo_gateway("shop-lb"), "/auth/demo-login", json={"sub": "alex.rivera"}))
    chat = run(post(echo_gateway("shop-lb"), "/v1/chat", json={}))
    assert login.status_code == 200 and login.json()["path"] == "/auth/demo-login"
    assert_local_gateway_denial(chat, 401)


def test_no_route_is_404_from_the_gateway(echo_gateway):
    response = run(post(echo_gateway("shop-lb"), "/auth/token"))  # only partner-lb routes /auth/token
    assert_local_gateway_denial(response, 404)


def test_ops_lb_rewrites_the_prefix_and_checks_the_role(echo_gateway, token):
    associate = token({"sub": "jordan.lee", "role": "store_associate", "aud": "copperline-ops"})
    allowed = run(post(echo_gateway("ops-lb"), "/storeops/v1/chat?x=1", token=associate, json={}))
    denied = run(post(echo_gateway("ops-lb"), "/refunds/v1/chat", token=associate, json={}))
    assert allowed.status_code == 200
    assert (allowed.json()["path"], allowed.json()["query"]) == ("/v1/chat", "x=1")
    assert_local_gateway_denial(denied, 403)


def test_llm_lb_replaces_any_client_api_key_with_the_gateway_key(echo_gateway, token):
    claims = {**TOOL_CLAIMS, "aud": "copperline-llm"}
    response = run(post(echo_gateway("llm-lb"), "/v1/converse", token=token(claims),
                        headers={"x-model-route": "fast", "X-Api-Key": "stolen", "x-api-key": "stolen-2"}, json={}))
    assert response.status_code == 200
    keys = [value for name, value in response.json()["headers"] if name == "x-api-key"]
    assert keys == ["test-gateway-key"]


def test_erp_lb_never_routes_admin(echo_gateway, token):
    svc = token({"sub": "mcp-servers", "svc_role": "tool-server", "aud": "copperline-erp"})
    async def get(path):
        async with echo_gateway("erp-lb") as client:
            return await client.get(path, headers={"Authorization": f"Bearer {svc}"})
    assert run(get("/orders/CO-1042")).status_code == 200
    assert_local_gateway_denial(run(get("/admin/state")), 403)


def test_an_encoded_question_mark_cannot_change_the_judged_path(echo_gateway, token):
    """The pool must see the same path the policy judged, not a path plus a smuggled query."""
    svc = token({"sub": "mcp-servers", "svc_role": "tool-server", "aud": "copperline-erp"})
    async def get(path):
        async with echo_gateway("erp-lb") as client:
            return await client.get(path, headers={"Authorization": f"Bearer {svc}"})
    echoed = run(get("/orders/CO-1042%3Fx=1?y=2")).json()
    assert (echoed["path"], echoed["query"]) == ("/orders/CO-1042?x=1", "y=2")


def test_dot_segments_are_refused(echo_gateway):
    async def get():
        async with echo_gateway("idp-lb") as client:
            return await client.get("/.well-known/%2e%2e/auth/token")
    assert_local_gateway_denial(run(get()), 400)


def test_unreachable_pool_is_503_from_the_gateway(unreachable_gateway, token):
    response = run(post(unreachable_gateway("tools-lb"), "/mcp/orders", token=token(TOOL_CLAIMS), headers=ORDER_STATUS))
    assert response.status_code == 503 and response.headers["x-local-gateway"] == "tools-lb"
    assert ORIGIN_HEADER not in response.headers


def test_health_path_is_the_gateways_own(echo_gateway):
    async def get():
        async with echo_gateway("tools-lb") as client:
            return await client.get(HEALTH_PATH)
    response = run(get())
    assert response.status_code == 200 and json.loads(response.text)["lb"] == "tools-lb"


def test_starts_and_stops_with_its_own_client_like_in_the_container(jwks_path, monkeypatch):
    """No injected client: the code path `python -m local_gateway` takes, including startup and shutdown."""
    from pathlib import Path

    from local_gateway.app import Settings, create_app
    from starlette.testclient import TestClient

    repo = Path(__file__).resolve().parents[4]
    monkeypatch.setenv("LB_NAME", "tools-lb")
    monkeypatch.setenv("INTENT_DIR", str(repo / "deploy/xc/intent"))
    monkeypatch.setenv("POOLS_FILE", str(repo / "deploy/local/pools.yaml"))
    monkeypatch.setenv("JWKS_PATH", str(jwks_path))
    with TestClient(create_app(Settings.from_env())) as client:  # runs the lifespan on enter and exit
        assert client.get(HEALTH_PATH).json()["lb"] == "tools-lb"


def test_a_rotated_key_is_picked_up_from_the_jwks_file(echo_gateway, jwks_path, token):
    """gen_keys.py --rotate rewrites jwks.json; a RUNNING gateway must accept the new key without a restart."""
    from copperline_common.jwt import public_jwks

    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async def go():
        async with echo_gateway("tools-lb") as client:  # one gateway instance for both requests
            before = token(TOOL_CLAIMS)
            old = await client.post("/mcp/orders", headers={**ORDER_STATUS, "Authorization": f"Bearer {before}"})
            jwks_path.write_text(json.dumps(public_jwks(new_key)))
            after = token(TOOL_CLAIMS, key=new_key)
            new = await client.post("/mcp/orders", headers={**ORDER_STATUS, "Authorization": f"Bearer {after}"})
            return old.status_code, new.status_code

    assert run(go()) == (200, 200)


def test_two_authorization_headers_are_refused(echo_gateway, token):
    """The pool must never get a second, unjudged token next to the one the gateway validated."""
    headers = [("authorization", f"Bearer {token(TOOL_CLAIMS)}"), ("authorization", "Bearer forged"),
               *ORDER_STATUS.items()]
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", headers=raw_headers(headers)))
    assert_local_gateway_denial(response, 401)
    assert "repeated Authorization" in response.json()["reason"]


def test_a_policy_header_sent_twice_is_denied_over_http(echo_gateway, token):
    """Forbidden name first, allowed name second: judging only one copy would let the shopper read card details."""
    headers = [("authorization", f"Bearer {token(TOOL_CLAIMS)}"), ("mcp-method", "tools/call"),
               ("mcp-name", "get_order_details"), ("mcp-name", "get_order_status")]
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", headers=raw_headers(headers)))
    assert_local_gateway_denial(response, 403)


def test_llm_lb_policy_denies_a_route_the_matrix_does_not_grant(echo_gateway, token):
    claims = {**TOOL_CLAIMS, "aud": "copperline-llm"}
    response = run(post(echo_gateway("llm-lb"), "/v1/converse", token=token(claims),
                        headers={"x-model-route": "premium"}, json={}))
    assert_local_gateway_denial(response, 403)
    assert "sp-llm" in response.json()["reason"]


def test_request_headers_to_remove_works_on_its_own(tmp_path, echo_gateway, token):
    """llm-lb removes and adds the same header, so its test cannot tell the two steps apart. Remove-only here."""
    import shutil

    import yaml

    intent = tmp_path / "intent"
    shutil.copytree(Path(__file__).resolve().parents[4] / "deploy/xc/intent", intent)
    lbs = yaml.safe_load((intent / "20-load-balancers.yaml").read_text())
    tools_lb = next(lb for lb in lbs["http_load_balancers"] if lb["name"] == "tools-lb")
    tools_lb["request_headers_to_remove"] = ["x-debug-token"]
    (intent / "20-load-balancers.yaml").write_text(yaml.safe_dump(lbs))

    response = run(post(echo_gateway("tools-lb", intent), "/mcp/orders", token=token(TOOL_CLAIMS),
                        headers={**ORDER_STATUS, "X-Debug-Token": "secret"}))
    assert response.status_code == 200
    assert "x-debug-token" not in dict(response.json()["headers"])


def test_pool_timeout_is_504_from_the_gateway(unreachable_gateway, token):
    response = run(post(unreachable_gateway("tools-lb", timeout=True), "/mcp/orders", token=token(TOOL_CLAIMS),
                        headers=ORDER_STATUS))
    assert response.status_code == 504 and response.headers["x-local-gateway"] == "tools-lb"
    assert ORIGIN_HEADER not in response.headers


def test_each_request_logs_one_lb_request_line(echo_gateway, unreachable_gateway, token, capsys):
    run(post(echo_gateway("tools-lb"), "/mcp/orders", token=token(TOOL_CLAIMS), headers=ORDER_STATUS))
    run(post(echo_gateway("tools-lb"), "/mcp/refunds", token=token(TOOL_CLAIMS),
             headers={"Mcp-Method": "tools/call", "Mcp-Name": "issue_refund"}))
    run(post(unreachable_gateway("tools-lb"), "/mcp/orders", token=token(TOOL_CLAIMS), headers=ORDER_STATUS))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"lb_request"' in line]
    assert [(line["status"], line["decision"]) for line in lines] == [
        (200, "allowed"), (403, "denied"), (503, "upstream_error")]
    allowed, denied, failed = lines
    assert allowed["policy_rule"] == "sp-tools/allow-shopper-orders-get-order-status"
    assert allowed["pool"] == "op-mcp-orders" and allowed["agent_id"] == "shopper"
    assert denied["policy_rule"] == "sp-tools/default-deny" and "pool" not in denied
    assert failed["policy_rule"] == "sp-tools/allow-shopper-orders-get-order-status"  # the policy did allow it


def test_an_empty_request_id_is_replaced_and_forwarded_once(echo_gateway, token):
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", token=token(TOOL_CLAIMS),
                        headers={**ORDER_STATUS, "x-request-id": ""}))
    sent = [value for name, value in response.json()["headers"] if name == "x-request-id"]
    assert len(sent) == 1 and sent[0]


def test_a_given_request_id_reaches_the_pool_exactly_once(echo_gateway, token):
    headers = [("authorization", f"Bearer {token(TOOL_CLAIMS)}"), *ORDER_STATUS.items(),
               ("x-request-id", "rid-a"), ("x-request-id", "rid-b")]
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", headers=raw_headers(headers)))
    assert [value for name, value in response.json()["headers"] if name == "x-request-id"] == ["rid-a"]


def test_a_non_ascii_header_value_is_forwarded_not_a_crash(echo_gateway, token):
    """uvicorn accepts obs-text bytes in header values; the gateway must forward them, not answer an unlabelled 500."""
    headers = [("authorization", f"Bearer {token(TOOL_CLAIMS)}"), *ORDER_STATUS.items(),
               ("user-agent", "agent (café)".encode())]
    response = run(post(echo_gateway("tools-lb"), "/mcp/orders", headers=raw_headers(headers)))
    assert response.status_code == 200
    raw = dict(response.json()["headers_raw"])
    assert raw["user-agent"] == "agent (café)".encode().hex()


def test_an_unrouted_method_gets_the_labelled_shape(echo_gateway):
    async def trace():
        async with echo_gateway("tools-lb") as client:
            return await client.request("TRACE", "/mcp/orders")
    assert_local_gateway_denial(run(trace()), 405)


def test_a_pool_cookie_is_never_replayed_to_another_caller(jwks_path, monkeypatch):
    """The DEFAULT client (the one the container uses) must not keep a cookie jar."""
    from local_gateway import app as gateway_app

    received = []

    class CookiePool(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            received.append(request.headers.get("cookie"))
            return httpx.Response(200, headers={"set-cookie": "session=ALEX; Path=/"}, json={})

    real_client = httpx.AsyncClient

    def client_with_pool(**kwargs):  # keep every option create_app passes; only swap the network for CookiePool
        return real_client(transport=CookiePool(), **kwargs)

    monkeypatch.setattr(gateway_app.httpx, "AsyncClient", client_with_pool)
    repo = Path(__file__).resolve().parents[4]
    settings = gateway_app.Settings(lb_name="shop-lb", intent_dir=repo / "deploy/xc/intent",
                                    pools_file=repo / "deploy/local/pools.yaml", jwks_path=jwks_path)
    app = gateway_app.create_app(settings)
    monkeypatch.setattr(gateway_app.httpx, "AsyncClient", real_client)

    async def two_callers():
        for _ in range(2):  # two separate clients: neither sends a cookie
            async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://lb") as caller:
                await caller.post("/auth/demo-login", json={"sub": "alex.rivera"})

    run(two_callers())
    assert received == [None, None]
