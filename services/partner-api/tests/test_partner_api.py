"""partner-api contract tests (services/partner-api/README.md, deploy/xc/openapi/partner-api.yaml)."""

import json
from pathlib import Path

import pytest
import yaml
from copperline_common.jwt import public_jwks, sign
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from partner_api import data
from partner_api.app import create_app
from partner_api.settings import Settings

REPO = Path(__file__).resolve().parents[3]
ISSUER = "https://idp.copperline.demo"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SPEC = yaml.safe_load((REPO / "deploy/xc/openapi/partner-api.yaml").read_text())


def token(**overrides) -> str:
    claims = {"sub": "granite-supply", "role": "partner", "aud": "copperline-partner"}
    claims.update(overrides)
    return sign(claims, KEY, ttl_s=300, issuer=overrides.pop("iss", ISSUER))


@pytest.fixture
def client(tmp_path):
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(public_jwks(KEY)))
    with TestClient(create_app(Settings(jwks_path=str(jwks)))) as test_client:
        yield test_client


def get(client, path, bearer=None, **kwargs):
    headers = {"Authorization": f"Bearer {bearer or token()}"} if bearer != "" else {}
    return client.get(path, headers=headers, **kwargs)


# ---- auth: re-checked here even though XC checks it first ------------------------------------------------------------


@pytest.mark.parametrize(
    "bearer, status",
    [
        ("", 401),
        ("not-a-jwt", 401),
        (None, 200),  # the default partner token
    ],
)
def test_a_partner_token_is_required(client, bearer, status):
    assert get(client, "/v1/purchase-orders", bearer).status_code == status


@pytest.mark.parametrize("overrides", [{"aud": "copperline-shop"}, {"iss": "https://evil.test"}])
def test_wrong_audience_or_issuer_is_401(client, overrides):
    response = get(client, "/v1/purchase-orders", token(**overrides))
    assert response.status_code == 401 and response.headers["x-copperline-origin"] == "partner-api"


def test_a_token_signed_by_another_key_is_401(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = sign({"sub": "granite-supply", "role": "partner", "aud": "copperline-partner"}, other, ttl_s=300,
                  issuer=ISSUER)
    assert get(client, "/v1/purchase-orders", forged).status_code == 401


def test_a_valid_token_without_the_partner_role_is_403(client):
    assert get(client, "/v1/purchase-orders", token(role="customer")).status_code == 403


def test_auth_runs_before_routing(client):
    """No token: 401 even for paths that do not exist, so an anonymous caller learns nothing about the API."""
    assert get(client, "/v1/nope", "").status_code == 401
    assert get(client, "/v1/internal/cost-sheet", "").status_code == 401


def test_healthz_needs_no_token(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "partner-api"}


# ---- documented operations -----------------------------------------------------------------------------------------


def test_purchase_orders_are_paged_25_at_a_time_and_deterministic(client):
    first = get(client, "/v1/purchase-orders").json()
    again = get(client, "/v1/purchase-orders?page=1").json()
    assert first == again and first["page_size"] == 25 and len(first["items"]) == 25
    assert first["total"] == data.PO_COUNT
    assert get(client, "/v1/purchase-orders?page=2").json()["items"][0]["po_id"] == "PO-5026"


@pytest.mark.parametrize("page", [1, 50, 100])  # S6(d) reads pages 1..50; the spec allows 1..100
def test_every_page_the_spec_allows_is_200(client, page):
    assert get(client, f"/v1/purchase-orders?page={page}").status_code == 200


@pytest.mark.parametrize("query", ["page=0", "page=101", "page=x", "status=lost"])
def test_out_of_spec_queries_are_422(client, query):
    assert get(client, f"/v1/purchase-orders?{query}").status_code == 422


def test_status_filter(client):
    items = get(client, "/v1/purchase-orders?status=open").json()["items"]
    assert items and {po["status"] for po in items} == {"open"}


def test_one_purchase_order(client):
    assert get(client, "/v1/purchase-orders/PO-5001").json()["po_id"] == "PO-5001"
    assert get(client, "/v1/purchase-orders/PO-9999").status_code == 404
    assert get(client, "/v1/purchase-orders/PO-50011").status_code == 404  # outside the spec pattern


def test_asn_is_202_and_echoes_po_id_even_for_the_s6_injection_string(client):
    """The WAF is meant to stop S6(c); if it does not, the service just stores a string. No SQL anywhere."""
    body = {"po_id": "PO-5531' OR '1'='1", "carrier": "UPS", "items": []}
    response = client.post("/v1/asn", json=body, headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 202
    assert response.json() == {"asn_id": "ASN-00001", "po_id": "PO-5531' OR '1'='1", "status": "accepted"}


@pytest.mark.parametrize("body", [{}, {"po_id": "PO-5001", "carrier": "x" * 33, "items": []},
                                  {"po_id": "PO-5001", "carrier": "UPS", "items": "nope"}])
def test_bad_asn_bodies_are_422(client, body):
    response = client.post("/v1/asn", json=body, headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 422


# ---- shadow operations -----------------------------------------------------------------------------------------------


def test_the_shadow_endpoints_exist_and_carry_sensitive_looking_data(client):
    """They must really answer (XC API discovery has to see them) and hold something sensitive-data discovery flags."""
    cost = get(client, "/v1/internal/cost-sheet").json()
    assert cost["remit_to"]["iban"].startswith("DE89") and "@" in cost["contact"]["email"]
    assert get(client, "/v1/internal/margins").json()["by_category"]


def test_the_published_spec_documents_exactly_the_three_operations_and_no_shadow_path():
    operations = {(path, method) for path, ops in SPEC["paths"].items() for method in ops}
    assert operations == {("/v1/purchase-orders", "get"), ("/v1/purchase-orders/{po_id}", "get"),
                          ("/v1/asn", "post")}


def test_the_app_implements_every_documented_operation_and_both_shadow_paths(client):
    routes = {(route.path, method.lower()) for route in client.app.routes for method in getattr(route, "methods", ())}
    assert {("/v1/purchase-orders", "get"), ("/v1/purchase-orders/{po_id}", "get"), ("/v1/asn", "post"),
            ("/v1/internal/cost-sheet", "get"), ("/v1/internal/margins", "get")} <= routes


def test_settings_refuse_to_start_without_keys(monkeypatch):
    monkeypatch.delenv("JWKS_PATH", raising=False)
    monkeypatch.delenv("IDP_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="JWKS_PATH"):
        Settings.from_env()


# ---- step-8 review ---------------------------------------------------------------------------------------------------


def asn(client, body, bearer=None, headers=None):
    sent = {"Authorization": f"Bearer {bearer or token()}"}
    sent.update(headers or {})
    return client.post("/v1/asn", json=body, headers=sent) if isinstance(body, dict) else \
        client.post("/v1/asn", content=body, headers={**sent, "content-type": "application/json"})


def test_an_oversized_body_is_413_before_it_is_parsed(client):
    """One big ASN must not be able to push the pod past its 256Mi limit (it would take S6 down)."""
    from partner_api.app import MAX_BODY_BYTES

    big = b'{"po_id":"x","carrier":"UPS","items":[' + b"{}," * (MAX_BODY_BYTES // 3) + b"{}]}"
    response = asn(client, big)
    assert response.status_code == 413 and response.json()["error"] == "payload_too_large"


def test_a_chunked_body_with_no_length_is_capped_on_the_bytes_received():
    """The cap counts what arrives, so a body with no Content-Length (chunked) cannot get around it, and the rest of
    the stream is never read."""
    import asyncio

    from partner_api.app import MAX_BODY_BYTES

    app = make_app()
    chunks = [b'{"po_id":"x","carrier":"UPS","items":[' + b"{}," * 20_000] + [b"{}," * 20_000] * 50 + [b"{}]}"]
    pending, out = list(chunks), {}

    async def receive():
        if pending:
            body = pending.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]

    scope = {"type": "http", "http_version": "1.1", "method": "POST", "path": "/v1/asn", "raw_path": b"/v1/asn",
             "query_string": b"", "scheme": "http", "server": ("t", 80), "client": ("c", 1), "root_path": "",
             "headers": [(b"authorization", f"Bearer {token()}".encode()), (b"content-type", b"application/json")]}
    asyncio.run(app(scope, receive, send))
    assert out["status"] == 413
    assert len(pending) > 40  # stopped reading long before the end
    assert sum(len(c) for c in chunks) > 10 * MAX_BODY_BYTES


def test_a_body_sent_in_several_pieces_still_works(client):
    body = json.dumps({"po_id": "PO-5001", "carrier": "UPS", "items": [{"sku": "TNT-ALPINE-2", "qty": 4}]}).encode()

    def pieces():
        yield body[:10]
        yield body[10:40]
        yield body[40:]

    response = client.post("/v1/asn", content=pieces(), headers={"Authorization": f"Bearer {token()}",
                                                                  "content-type": "application/json"})
    assert response.status_code == 202


def test_a_repeated_authorization_header_is_401_whichever_copy_is_valid(client):
    """The request-ID middleware logs identity from the first copy; verifying a different copy would make the audit
    log name the wrong caller."""
    for headers in ([("authorization", f"Bearer {token()}"), ("authorization", "Bearer forged")],
                    [("authorization", "Bearer forged"), ("authorization", f"Bearer {token()}")],
                    [("authorization", f"Bearer {token()}")] * 2):
        response = client.get("/v1/purchase-orders", headers=headers)
        assert response.status_code == 401 and "repeated" in response.json()["detail"]


def test_asn_ids_never_repeat_after_the_store_is_full(client):
    from partner_api.app import MAX_ASNS

    body = {"po_id": "PO-5001", "carrier": "UPS", "items": []}
    ids = [asn(client, body).json()["asn_id"] for _ in range(MAX_ASNS + 5)]
    assert len(set(ids)) == len(ids) and ids[-1] == f"ASN-{MAX_ASNS + 5:05d}"
    assert len(client.app.state.asns) == MAX_ASNS  # still bounded


def make_app(**settings):
    import tempfile

    if "jwks_path" not in settings and "idp_base_url" not in settings:
        path = Path(tempfile.mkdtemp()) / "jwks.json"
        path.write_text(json.dumps(public_jwks(KEY)))
        settings["jwks_path"] = str(path)
    return create_app(Settings(**settings))


def test_unloadable_keys_are_503_with_no_internal_detail(capsys):
    """IdP unreachable through idp-lb: our fault (503), and the outside caller learns nothing about our network."""
    client = TestClient(make_app(idp_base_url="http://127.0.0.1:9"))  # nothing listens on port 9
    response = client.get("/v1/purchase-orders", headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 503 and response.json() == {"error": "unavailable",
                                                               "detail": "token keys unavailable"}
    assert "127.0.0.1" in capsys.readouterr().out  # the real reason is in the log for the SE


def test_keys_are_fetched_from_the_idp_url_as_in_compose_and_vk8s():
    """IDP_BASE_URL mode is the only one the containers use: keys from <idp>/.well-known/jwks.json, issuer checked."""
    import httpx

    seen = []

    def idp(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=public_jwks(KEY))

    settings = Settings(idp_base_url="http://idp.agentic-demo/")
    assert settings.jwks_source() == "http://idp.agentic-demo/.well-known/jwks.json"
    verifier = settings.verifier()
    import copperline_common.jwt as jwt_module

    real_get = jwt_module.httpx.get
    jwt_module.httpx.get = lambda url, **kw: httpx.Client(transport=httpx.MockTransport(idp)).get(url, **kw)
    try:
        assert verifier.verify(token(), "copperline-partner")["sub"] == "granite-supply"
        with pytest.raises(Exception, match="issuer"):
            verifier.verify(token(iss="https://evil.test"), "copperline-partner")
    finally:
        jwt_module.httpx.get = real_get
    assert seen == ["http://idp.agentic-demo/.well-known/jwks.json"]


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("IDP_BASE_URL", "http://idp.agentic-demo")
    monkeypatch.setenv("ISSUER", "https://issuer.test")
    monkeypatch.setenv("PORT", "9090")
    monkeypatch.delenv("JWKS_PATH", raising=False)
    s = Settings.from_env()
    assert (s.idp_base_url, s.issuer, s.port, s.jwks_path) == ("http://idp.agentic-demo", "https://issuer.test",
                                                               9090, None)
    monkeypatch.setenv("JWKS_PATH", "/keys/jwks.json")
    assert Settings.from_env().jwks_source() == "/keys/jwks.json"  # a file wins over the URL


def test_the_purchase_orders_are_the_same_on_every_start():
    """Regenerating must give the same data: a dropped or changed seed would change POs after every restart."""
    assert data._purchase_orders() == data.PURCHASE_ORDERS
    assert data.BY_ID["PO-5001"] == {  # pinned: a different seed changes these
        "po_id": "PO-5001", "supplier": "granite-supply", "status": "open", "due": "2026-11-18",
        "lines": [{"sku": "TNT-ALPINE-2", "qty": 92}, {"sku": "SOK-MERINO-3", "qty": 104},
                  {"sku": "STV-CAMP-1", "qty": 39}]}


def test_asn_limits_match_the_published_spec(client):
    """XC validates against the spec and the service enforces the same limits, so neither rejects what the other
    accepts."""
    from partner_api.app import MAX_ITEMS, MAX_PO_ID

    asn_schema = SPEC["paths"]["/v1/asn"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert asn_schema["properties"]["po_id"]["maxLength"] == MAX_PO_ID
    assert asn_schema["properties"]["items"]["maxItems"] == MAX_ITEMS
    assert asn(client, {"po_id": "x" * MAX_PO_ID, "carrier": "UPS", "items": [{}] * MAX_ITEMS}).status_code == 202
    assert asn(client, {"po_id": "x" * (MAX_PO_ID + 1), "carrier": "UPS", "items": []}).status_code == 422
