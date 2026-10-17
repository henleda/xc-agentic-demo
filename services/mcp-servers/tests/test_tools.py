"""Tool behaviour: object-level authorization, masking, catalog, ERP error mapping, token handling, log hygiene."""

import json

import httpx
import pytest
from mcp_servers.app import Settings, create_app

ALEX = ("shopper", "customer", "alex.rivera")


# ---- object-level authorization (customers see only their own orders) -----------------------------------------


def test_customer_cannot_read_someone_elses_order(server, agent_token, mcp):
    orders, _ = server("orders")
    assert mcp.tool_error(mcp.call(orders, "orders", "get_order_status", {"order_id": "CO-1077"},
                                   agent_token(*ALEX))) == "not your order"


def test_get_order_details_rule_holds_even_for_a_customer_token_the_matrix_never_grants(make_world, config_dir,
                                                                                         agent_token, mcp):
    """No agent may call get_order_details for a customer today (role check -> 403 first). If the matrix ever grants
    it, the object-level rule must still apply, so exercise the handler directly."""
    import asyncio

    from copperline_common.mcp import McpRequestContext, ToolError
    from copperline_common.personas import Personas
    from mcp_servers.erp import ErpClient
    from mcp_servers.tools import orders_handlers

    world = make_world()
    erp = ErpClient(world.http, erp_base_url="http://erp.test", idp_base_url="http://idp.test",
                    client_id="svc-mcp", client_secret="test-svc-secret")
    details = orders_handlers(erp, Personas.load(config_dir / "personas.yaml"))["get_order_details"]
    ctx = McpRequestContext(method="tools/call", tool="get_order_details", token="t",
                            claims={"obo_role": "customer", "obo_sub": "alex.rivera"}, request_id="r")
    with pytest.raises(ToolError, match="not your order"):
        asyncio.run(details({"order_id": "CO-1077"}, ctx))


def test_unknown_order_gives_a_customer_the_same_message(server, agent_token, mcp):
    orders, _ = server("orders")
    assert mcp.tool_error(mcp.call(orders, "orders", "get_order_status", {"order_id": "CO-9999"},
                                   agent_token(*ALEX))) == "not your order"


def test_salted_mallory_reads_her_own_order(server, agent_token, mcp):
    orders, _ = server("orders")
    status = mcp.result(mcp.call(orders, "orders", "get_order_status", {"order_id": "CO-1077"},
                                 agent_token("shopper", "customer", "mallory.x-ab12")))
    assert status["order_id"] == "CO-1077"


@pytest.mark.parametrize("obo_sub", ["mallory.x-alex.rivera", "alex.rivera-ab12", "ALEX.RIVERA", "nobody"])
def test_salt_and_case_tricks_do_not_borrow_alexs_orders(server, agent_token, mcp, obo_sub):
    orders, _ = server("orders")
    resp = mcp.call(orders, "orders", "get_order_status", {"order_id": "CO-1042"},
                    agent_token("shopper", "customer", obo_sub))
    assert mcp.tool_error(resp) == "not your order"


def test_staff_have_no_object_level_restriction(server, agent_token, mcp):
    orders, _ = server("orders")
    details = mcp.result(mcp.call(orders, "orders", "get_order_details", {"order_id": "CO-1077"},
                                  agent_token("refunds", "care_agent", "sam.patel")))
    assert details["customer_id"] == "C-1066"


@pytest.mark.parametrize("order_id", ["../admin/state", "CO-1042/../../admin/state", "%2e%2e/admin", "CO-1042?x=1"])
def test_order_id_cannot_change_the_erp_path(server, agent_token, mcp, order_id):
    orders, world = server("orders")
    resp = mcp.call(orders, "orders", "get_order_details", {"order_id": order_id},
                    agent_token("refunds", "care_agent", "sam.patel"))
    assert mcp.tool_error(resp) == "order not found"
    assert all(request.url.path.startswith("/orders/") for request in world.erp_calls)


@pytest.mark.parametrize("server_name, tool, args", [
    ("orders", "get_order_details", {"order_id": ".."}),
    ("orders", "get_order_details", {"order_id": "."}),
    ("orders", "list_customer_orders", {"customer_id": "."}),
    ("refunds", "get_refund_status", {"refund_id": ""}),
    ("inventory", "check_stock", {"sku": ".."}),
])
def test_dot_and_empty_ids_never_reach_the_erp(server, agent_token, mcp, server_name, tool, args):
    """quote() leaves '.' and '..' alone and httpx then collapses them, which would call a different ERP route."""
    roles = {"orders": ("refunds", "care_agent"), "refunds": ("refunds", "care_agent"),
             "inventory": ("storeops", "store_associate")}
    client, world = server(server_name)
    message = mcp.tool_error(mcp.call(client, server_name, tool, args, agent_token(*roles[server_name])))
    assert message.startswith("invalid request")
    assert world.erp_calls == []


@pytest.mark.parametrize("raw_amount", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_amounts_never_reach_the_erp(server, agent_token, raw_amount):
    refunds, world = server("refunds")
    raw = ('{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"issue_refund",'
           '"arguments":{"order_id":"CO-1042","amount":' + raw_amount + '},'
           '"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",'
           '"io.modelcontextprotocol/clientCapabilities":{}}}}')
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "issue_refund",
               "Content-Type": "application/json",
               "Authorization": f"Bearer {agent_token('refunds', 'care_supervisor')}"}
    resp = refunds.post("/mcp/refunds", content=raw, headers=headers)
    assert resp.status_code in (200, 400), resp.text  # a parse error (400) or an isError result, never a 500
    if resp.status_code == 200:
        assert resp.json()["result"]["isError"] is True
    else:
        assert resp.json()["error"]["code"] == -32700
    assert world.erp_calls == []


# ---- results --------------------------------------------------------------------------------------------------


def test_order_status_never_contains_pan_or_email(server, agent_token, mcp):
    orders, _ = server("orders")
    text = mcp.call(orders, "orders", "get_order_status", {"order_id": "CO-1042"}, agent_token(*ALEX)).text
    assert "4111111111111111" not in text and "alex.rivera@example.com" not in text and "549" not in text


def test_bulk_export_returns_every_order_for_staff(server, agent_token, mcp):
    orders, _ = server("orders")
    body = mcp.result(mcp.call(orders, "orders", "list_customer_orders", {"customer_id": "*"},
                               agent_token("refunds", "care_supervisor", "morgan.chen")))
    assert body["count"] == 200 and len(body["orders"]) == 200


def test_catalog_search_limits(server, agent_token, mcp):
    catalog, _ = server("catalog")
    token = agent_token(*ALEX)
    assert len(mcp.result(mcp.call(catalog, "catalog", "search_products", {"query": "a"}, token))["results"]) == 10
    big = mcp.result(mcp.call(catalog, "catalog", "search_products", {"query": "a", "limit": 100}, token))
    assert len(big["results"]) == 25
    tent = mcp.result(mcp.call(catalog, "catalog", "search_products", {"query": "alpine 2"}, token))["results"]
    assert {"sku": "TNT-ALPINE-2", "name": "Alpine 2 Tent", "category": tent[0]["category"], "price": 549.0} in tent


def test_catalog_get_product_and_unknown(server, agent_token, mcp):
    catalog, _ = server("catalog")
    product = mcp.result(mcp.call(catalog, "catalog", "get_product", {"product_id": "TNT-ALPINE-2"},
                                  agent_token(*ALEX)))
    assert product["review_count"] == 5 and "reviews" not in product
    assert mcp.tool_error(mcp.call(catalog, "catalog", "get_product", {"product_id": "NOPE"},
                                   agent_token(*ALEX))) == "product not found"


def test_bad_arguments_are_a_tool_error_the_model_can_read(server, agent_token, mcp):
    inventory, world = server("inventory")
    args = {"sku": "TNT-ALPINE-2", "store_id": "S-001", "delta": "two"}
    resp = mcp.call(inventory, "inventory", "adjust_stock", args, agent_token("storeops", "store_associate"))
    assert mcp.tool_error(resp) and world.erp_calls == []


# ---- ERP error mapping ----------------------------------------------------------------------------------------


def test_insufficient_stock_is_a_tool_error(server, agent_token, mcp):
    inventory, _ = server("inventory")
    args = {"sku": "TNT-ALPINE-2", "store_id": "S-001", "delta": -1000}
    resp = mcp.call(inventory, "inventory", "adjust_stock", args, agent_token("storeops", "store_associate"))
    assert mcp.tool_error(resp).startswith("insufficient stock")


def test_unknown_refund_is_a_tool_error(server, agent_token, mcp):
    refunds, _ = server("refunds")
    resp = mcp.call(refunds, "refunds", "get_refund_status", {"refund_id": "RF-9999"},
                    agent_token("refunds", "care_agent"))
    assert mcp.tool_error(resp) == "refund not found"


def test_erp_down_is_502(server, agent_token, mcp):
    refunds, _ = server("refunds", erp_down=True)
    resp = mcp.call(refunds, "refunds", "issue_refund", {"order_id": "CO-1042", "amount": 120},
                    agent_token("refunds", "care_supervisor"))
    assert resp.status_code == 502 and resp.json()["error"]["code"] == -32603


def _fake_world(config_dir, erp_handler):
    """An MCP refunds server whose IdP and ERP are plain functions, for edge cases the real apps don't produce."""
    seen = {"token": 0, "erp": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            seen["token"] += 1
            return httpx.Response(200, json={"access_token": f"svc-token-{seen['token']}", "expires_in": 300})
        seen["erp"].append(request.headers["authorization"])
        return erp_handler(len(seen["erp"]))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(server="refunds", config_dir=config_dir, erp_base_url="http://erp.test",
                        idp_base_url="http://idp.test", svc_client_secret="s")
    return create_app(settings, http=http), seen


def test_erp_401_refreshes_the_token_exactly_once(config_dir, agent_token, mcp):
    from fastapi.testclient import TestClient

    app, seen = _fake_world(config_dir, lambda n: httpx.Response(401) if n == 1 else
                            httpx.Response(201, json={"refund_id": "RF-0001", "status": "issued"}))
    with TestClient(app) as client:
        mcp.result(mcp.call(client, "refunds", "issue_refund", {"order_id": "CO-1042", "amount": 1},
                            agent_token("refunds", "care_supervisor")))
    assert seen["token"] == 2 and seen["erp"] == ["Bearer svc-token-1", "Bearer svc-token-2"]


def test_erp_401_twice_is_502_not_a_loop(config_dir, agent_token, mcp):
    from fastapi.testclient import TestClient

    app, seen = _fake_world(config_dir, lambda n: httpx.Response(401))
    with TestClient(app) as client:
        resp = mcp.call(client, "refunds", "issue_refund", {"order_id": "CO-1042", "amount": 1},
                        agent_token("refunds", "care_supervisor"))
    assert resp.status_code == 502 and len(seen["erp"]) == 2 and seen["token"] == 2


# ---- authorization edge cases ---------------------------------------------------------------------------------


def test_missing_or_garbage_token_is_401_and_logged(server, mcp, capsys):
    orders, world = server("orders")
    for token in (None, "not-a-jwt"):
        resp = mcp.call(orders, "orders", "get_order_status", {"order_id": "CO-1042"}, token)
        assert resp.status_code == 401 and resp.json()["error"]["code"] == -32001
    assert world.erp_calls == []
    denials = [json.loads(line) for line in capsys.readouterr().out.splitlines()
               if line.startswith("{") and '"server_authz_deny"' in line]
    assert [d["status"] for d in denials] == [401, 401]
    assert denials[0]["reason"] == "no bearer token" and "not-a-jwt" not in json.dumps(denials)


def test_token_without_role_claims_is_401(server, mcp, rsa_key):
    from copperline_common.jwt import sign

    orders, _ = server("orders")
    token = sign({"sub": "x", "aud": "copperline-tools"}, rsa_key, ttl_s=60, issuer="https://idp.copperline.demo")
    assert mcp.call(orders, "orders", "get_order_status", {"order_id": "CO-1042"}, token).status_code == 401


def test_mismatch_beats_authorization(server, agent_token, mcp):
    """S1b ordering: a forbidden role AND a header mismatch must report the mismatch, not the 403."""
    refunds, _ = server("refunds")
    resp = mcp.call(refunds, "refunds", "issue_refund", {"order_id": "CO-1077", "amount": 499},
                    agent_token("shopper", "customer"), header_name="get_refund_status")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32020


# ---- process, health, logs ------------------------------------------------------------------------------------


def test_health_and_unknown_paths(server):
    orders, _ = server("orders")
    assert orders.get("/healthz").json() == {"status": "ok", "service": "mcp-orders"}
    assert orders.post("/mcp/refunds", json={}).status_code == 404


@pytest.mark.parametrize("settings_kwargs, message", [
    ({"server": "billing"}, "MCP_SERVER"),
    ({"server": "orders"}, "ERP_BASE_URL"),
    ({"server": "refunds", "erp_base_url": "http://e", "idp_base_url": "http://i"}, "SVC_CLIENT_SECRET"),
])
def test_refuses_to_start_half_configured(config_dir, settings_kwargs, message):
    with pytest.raises(RuntimeError, match=message):
        create_app(Settings(config_dir=config_dir, **settings_kwargs))


def test_catalog_needs_no_erp_settings(config_dir):
    create_app(Settings(server="catalog", config_dir=config_dir))


def test_no_pan_email_or_token_in_logs(server, agent_token, mcp, capsys):
    orders, _ = server("orders")
    token = agent_token("refunds", "care_agent", "sam.patel")
    mcp.result(mcp.call(orders, "orders", "get_order_details", {"order_id": "CO-1042"}, token))
    mcp.result(mcp.call(orders, "orders", "list_customer_orders", {"customer_id": "*"}, token))
    out = capsys.readouterr().out
    assert "4111111111111111" not in out and "alex.rivera@example.com" not in out and token not in out
    events = [json.loads(line)["event"] for line in out.splitlines() if line.startswith("{")]
    assert "tool_call" in events
