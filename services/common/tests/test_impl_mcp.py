import asyncio
import json

import httpx
import pytest
from copperline_common import mcp
from copperline_common.outbound import HttpResult
from copperline_common.service import setup_service
from copperline_common.tool_schemas import ToolDef, shorthand_to_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient

ORDERS_TOOLS = [
    ToolDef("orders", "get_order_status", "Status", shorthand_to_schema({"order_id": "string"})),
    ToolDef("orders", "get_order_details", "Details (PII)", shorthand_to_schema({"order_id": "string"})),
]
META = {
    mcp.META_PROTOCOL_VERSION: mcp.PROTOCOL_VERSION,
    mcp.META_CLIENT_CAPABILITIES: {},
    mcp.META_CLIENT_INFO: {"name": "t", "version": "1"},
}


class Recorder:
    """Records which hooks and handlers ran, so tests can prove that nothing ran."""

    def __init__(self, deny: mcp.McpDenied | None = None) -> None:
        self.authorize_calls: list[mcp.McpRequestContext] = []
        self.handler_calls: list[tuple[str, dict]] = []
        self.deny = deny

    async def authorize(self, ctx: mcp.McpRequestContext) -> None:
        self.authorize_calls.append(ctx)
        if self.deny is not None:
            raise self.deny

    def handler(self, name: str, behaviour=None):
        async def run(args: dict, ctx: mcp.McpRequestContext):
            self.handler_calls.append((name, args))
            if behaviour is not None:
                return behaviour(args)
            return {"order_id": args["order_id"], "status": "shipped"}

        return run


def make_app(recorder: Recorder, details_behaviour=None) -> FastAPI:
    app = FastAPI()
    setup_service(app, "mcp-orders")
    handlers = {
        "get_order_status": recorder.handler("get_order_status"),
        "get_order_details": recorder.handler("get_order_details", details_behaviour),
    }
    app.include_router(
        mcp.mcp_router(
            server_name="orders", path="/mcp/orders", tools=ORDERS_TOOLS, handlers=handlers,
            authorize=recorder.authorize,
        )
    )
    return app


def rpc(method: str, params: dict | None = None, *, meta: dict | None = META) -> dict:
    params = dict(params or {})
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": "req-1", "method": method, "params": params}


def headers(method: str, name: str | None = None, version: str = mcp.PROTOCOL_VERSION, **extra) -> dict:
    out = {"MCP-Protocol-Version": version, "Mcp-Method": method, "Authorization": "Bearer t", **extra}
    if name is not None:
        out["Mcp-Name"] = name
    return out


def call_body(tool: str, args: dict | None = None) -> dict:
    return rpc("tools/call", {"name": tool, "arguments": args if args is not None else {"order_id": "CO-1042"}})


# --- check order -------------------------------------------------------------------------------


def test_s1b_name_mismatch_is_400_and_authorize_never_runs():
    recorder = Recorder()
    client = TestClient(make_app(recorder))
    response = client.post(
        "/mcp/orders", json=call_body("get_order_details"), headers=headers("tools/call", "get_order_status")
    )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-copperline-origin"] == "mcp-orders"
    body = response.json()
    assert body["id"] == "req-1"
    assert body["error"]["code"] == -32020 == mcp.HEADER_MISMATCH
    assert body["error"]["message"] == "HeaderMismatch"
    assert body["error"]["data"] == {"header": "Mcp-Name"}
    assert recorder.authorize_calls == [] and recorder.handler_calls == []


def test_mismatch_beats_a_denying_authorize_hook():
    """Even a hook that would 403 is never consulted when the headers disagree with the body."""
    recorder = Recorder(deny=mcp.McpDenied(403, mcp.FORBIDDEN, "Forbidden"))
    client = TestClient(make_app(recorder))
    response = client.post(
        "/mcp/orders", json=call_body("get_order_details"), headers=headers("tools/call", "get_order_status")
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == mcp.HEADER_MISMATCH
    assert recorder.authorize_calls == []


@pytest.mark.parametrize(
    "body, hdrs, expected_header",
    [
        (call_body("get_order_status"), {"Mcp-Method": "tools/call", "Mcp-Name": "get_order_status"},
         "MCP-Protocol-Version"),
        (call_body("get_order_status"), headers("tools/call", "get_order_status", version="2025-11-25"),
         "MCP-Protocol-Version"),
        (call_body("get_order_status"), headers("tools/list", "get_order_status"), "Mcp-Method"),
        (call_body("get_order_status"), {**headers("tools/call"), "Mcp-Name": "Get_Order_Status"}, "Mcp-Name"),
        (call_body("get_order_status"), headers("tools/call"), "Mcp-Name"),
    ],
)
def test_header_mismatches(body, hdrs, expected_header):
    recorder = Recorder()
    response = TestClient(make_app(recorder)).post("/mcp/orders", json=body, headers=hdrs)
    assert response.status_code == 400
    expected = {"code": -32020, "message": "HeaderMismatch", "data": {"header": expected_header}}
    assert response.json()["error"] == expected
    assert recorder.authorize_calls == []


def test_header_names_are_case_insensitive():
    recorder = Recorder()
    hdrs = {"mcp-protocol-version": "2026-07-28", "MCP-METHOD": "tools/call", "mcp-name": "get_order_status"}
    response = TestClient(make_app(recorder)).post("/mcp/orders", json=call_body("get_order_status"), headers=hdrs)
    assert response.status_code == 200 and response.json()["result"]["isError"] is False


def test_unsupported_version_without_body_meta():
    recorder = Recorder()
    response = TestClient(make_app(recorder)).post(
        "/mcp/orders", json=rpc("tools/list", meta=None), headers=headers("tools/list", version="2099-01-01")
    )
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": -32022,
        "message": "UnsupportedProtocolVersion",
        "data": {"supported": ["2026-07-28"], "requested": "2099-01-01"},
    }
    assert recorder.authorize_calls == []


@pytest.mark.parametrize(
    "raw, code",
    [
        (b"{not json", mcp.PARSE_ERROR),
        (b"[1, 2]", mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "1.0", "id": 1, "method": "tools/list"}', mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1, "method": 7}', mcp.INVALID_REQUEST),
    ],
)
def test_envelope_errors_come_first(raw, code):
    recorder = Recorder()
    response = TestClient(make_app(recorder)).post(
        "/mcp/orders", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400 and response.json()["error"]["code"] == code
    assert recorder.authorize_calls == []


@pytest.mark.parametrize("status, code", [(401, mcp.UNAUTHORIZED), (403, mcp.FORBIDDEN)])
def test_authorize_denial(status, code, make_token):
    recorder = Recorder(deny=mcp.McpDenied(status, code, "Denied"))
    token = make_token(agent_id="shopper", agent_role="shopper", obo_sub="mallory.x-ab")
    response = TestClient(make_app(recorder)).post(
        "/mcp/orders",
        json=call_body("get_order_details"),
        headers=headers("tools/call", "get_order_details", Authorization=f"Bearer {token}"),
    )
    assert response.status_code == status and response.json()["error"]["code"] == code
    (ctx,) = recorder.authorize_calls
    assert ctx.method == "tools/call" and ctx.tool == "get_order_details" and ctx.token == token
    assert ctx.claims["agent_role"] == "shopper" and ctx.request_id
    assert recorder.handler_calls == []


def test_unknown_method_and_unknown_tool():
    recorder = Recorder()
    client = TestClient(make_app(recorder))
    unknown = client.post("/mcp/orders", json=rpc("prompts/list"), headers=headers("prompts/list"))
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == mcp.METHOD_NOT_FOUND
    no_tool = client.post("/mcp/orders", json=call_body("drop_tables"), headers=headers("tools/call", "drop_tables"))
    assert no_tool.status_code == 400 and no_tool.json()["error"]["code"] == mcp.INVALID_PARAMS
    assert len(recorder.authorize_calls) == 2 and recorder.handler_calls == []


# --- results -----------------------------------------------------------------------------------


def test_discover_and_list():
    client = TestClient(make_app(Recorder()))
    discover = client.post("/mcp/orders", json=rpc("server/discover"), headers=headers("server/discover")).json()
    assert discover["result"] == {
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"tools": {}},
        "resultType": "complete",
        "ttlMs": 60000,
        "cacheScope": "public",
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "orders", "version": "1.0"}},
    }
    listed = client.post("/mcp/orders", json=rpc("tools/list"), headers=headers("tools/list")).json()["result"]
    assert [t["name"] for t in listed["tools"]] == ["get_order_status", "get_order_details"]
    assert listed["tools"][0]["inputSchema"]["required"] == ["order_id"]
    assert listed["resultType"] == "complete" and listed["cacheScope"] == "public" and listed["ttlMs"] == 60000


def test_call_success_shapes():
    recorder = Recorder()
    response = TestClient(make_app(recorder)).post(
        "/mcp/orders", json=call_body("get_order_status"), headers=headers("tools/call", "get_order_status")
    )
    result = response.json()["result"]
    assert response.status_code == 200
    assert result["structuredContent"] == {"order_id": "CO-1042", "status": "shipped"}
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["content"][0]["type"] == "text" and result["isError"] is False and result["resultType"] == "complete"
    assert recorder.handler_calls == [("get_order_status", {"order_id": "CO-1042"})]


@pytest.mark.parametrize("value, structured", [(["a", "b"], {"result": ["a", "b"]}), ("plain", {"result": "plain"})])
def test_list_and_str_results_are_wrapped(value, structured):
    app = make_app(Recorder(), details_behaviour=lambda args: value)
    result = TestClient(app).post(
        "/mcp/orders", json=call_body("get_order_details"), headers=headers("tools/call", "get_order_details")
    ).json()["result"]
    assert result["structuredContent"] == structured


def test_bad_arguments_are_a_tool_error_not_a_protocol_error():
    recorder = Recorder()
    body = call_body("get_order_status", {"order_id": 42})
    hdrs = headers("tools/call", "get_order_status")
    response = TestClient(make_app(recorder)).post("/mcp/orders", json=body, headers=hdrs)
    result = response.json()["result"]
    assert response.status_code == 200 and result["isError"] is True and "order_id" in result["content"][0]["text"]
    assert recorder.handler_calls == []


def test_tool_error_and_upstream_error():
    def not_yours(args):
        raise mcp.ToolError("not your order")

    def erp_down(args):
        raise mcp.UpstreamError("ERP refused", upstream_status=403)

    hdrs = headers("tools/call", "get_order_details")
    tool_error = TestClient(make_app(Recorder(), not_yours)).post("/mcp/orders", json=call_body("get_order_details"),
                                                                  headers=hdrs)
    assert tool_error.status_code == 200
    assert tool_error.json()["result"]["isError"] is True
    assert tool_error.json()["result"]["content"][0]["text"] == "not your order"

    upstream = TestClient(make_app(Recorder(), erp_down)).post("/mcp/orders", json=call_body("get_order_details"),
                                                               headers=hdrs)
    assert upstream.status_code == 502
    assert upstream.json()["error"]["code"] == mcp.INTERNAL_ERROR
    assert upstream.json()["error"]["data"]["upstream_status"] == 403


def test_get_and_delete_are_405():
    client = TestClient(make_app(Recorder()))
    assert client.get("/mcp/orders").status_code == 405
    assert client.delete("/mcp/orders").status_code == 405


def test_reject_and_tool_call_are_logged(read_logs):
    client = TestClient(make_app(Recorder()))
    read_logs()
    client.post("/mcp/orders", json=call_body("get_order_details"), headers=headers("tools/call", "get_order_status"))
    client.post("/mcp/orders", json=call_body("get_order_status"), headers=headers("tools/call", "get_order_status"))
    records = read_logs()
    reject = [r for r in records if r.get("event") == "mcp_reject"][-1]
    assert reject["status"] == 400 and reject["code"] == -32020 and reject["tool"] == "get_order_details"
    call = [r for r in records if r.get("event") == "tool_call"][-1]
    assert call["tool"] == "get_order_status" and call["status"] == 200 and call["is_error"] is False


def test_router_rejects_missing_handlers():
    with pytest.raises(ValueError, match="without handlers"):
        mcp.mcp_router(server_name="orders", path="/mcp/orders", tools=ORDERS_TOOLS, handlers={})


# --- client ------------------------------------------------------------------------------------


def run_client(coro_factory, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await coro_factory(mcp.McpClient("http://tools.agentic-demo/", http))

    return asyncio.run(go())


def test_client_sends_headers_and_meta():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {
            "content": [{"type": "text", "text": "{\"ok\": true}"}], "isError": False}})

    result = run_client(lambda c: c.call_tool("orders", "get_order_details", {"order_id": "CO-1042"}, token="tok",
                                               header_name_override="get_order_status",
                                               extra_headers={"x-agent-id": "shopper"}), handler)
    request = seen[0]
    assert str(request.url) == "http://tools.agentic-demo/mcp/orders" and request.method == "POST"
    assert request.headers["MCP-Protocol-Version"] == "2026-07-28"
    assert request.headers["Mcp-Method"] == "tools/call"
    assert request.headers["Mcp-Name"] == "get_order_status"  # the override: body says get_order_details
    assert request.headers["Authorization"] == "Bearer tok" and request.headers["x-agent-id"] == "shopper"
    assert request.headers["Accept"] == "application/json, text/event-stream"
    body = json.loads(request.content)
    assert body["method"] == "tools/call" and body["params"]["name"] == "get_order_details"
    assert body["params"]["arguments"] == {"order_id": "CO-1042"}
    assert body["params"]["_meta"] == {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "copperline-agent", "version": "1.0"},
    }
    assert mcp.tool_result_text(result) == '{"ok": true}' and mcp.tool_result_is_error(result) is False


def test_client_list_and_discover_have_no_name_header():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {}})

    run_client(lambda c: c.list_tools("catalog", token=None), handler)
    run_client(lambda c: c.discover("catalog", token=None), handler)
    assert [r.headers["Mcp-Method"] for r in seen] == ["tools/list", "server/discover"]
    assert all("Mcp-Name" not in r.headers and "Authorization" not in r.headers for r in seen)


def test_client_retries_connect_error_once_but_never_http_errors():
    attempts = []

    def flaky(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(429, text="Too Many Requests")

    result = run_client(lambda c: c.call_tool("inventory", "adjust_stock", {}, token="t"), flaky)
    assert len(attempts) == 2 and result.status == 429
    assert mcp.tool_result_is_error(result) is None and mcp.jsonrpc_error_of(result) is None


def test_result_helpers_on_errors():
    error = HttpResult(400, {}, "", {"jsonrpc": "2.0", "id": 1, "error": {"code": -32020, "message": "HeaderMismatch"}},
                       3)
    assert mcp.jsonrpc_error_of(error)["code"] == -32020
    assert mcp.tool_result_text(error) is None and mcp.tool_result_is_error(error) is None
    assert mcp.jsonrpc_error(None, -32600, "Invalid Request") == {
        "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    assert mcp.jsonrpc_error(5, 1, "m", {"a": 1})["error"]["data"] == {"a": 1}


def test_client_against_real_router():
    """Round trip: our client against our server, in-process."""
    app = make_app(Recorder())
    transport = httpx.ASGITransport(app=app)

    async def go():
        async with httpx.AsyncClient(transport=transport) as http:
            client = mcp.McpClient("http://tools", http)
            ok = await client.call_tool("orders", "get_order_status", {"order_id": "CO-1"}, token="t")
            s1b = await client.call_tool("orders", "get_order_details", {"order_id": "CO-1"}, token="t",
                                         header_name_override="get_order_status")
            return ok, s1b

    ok, s1b = asyncio.run(go())
    assert ok.status == 200 and json.loads(mcp.tool_result_text(ok))["status"] == "shipped"
    assert s1b.status == 400 and mcp.jsonrpc_error_of(s1b)["data"] == {"header": "Mcp-Name"}
    assert s1b.headers["x-copperline-origin"] == "mcp-orders"


# --- review follow-ups: routing headers must be unambiguous before authorize runs -----------------------------


def _raw_post(client: TestClient, header_list: list[tuple[str, str]], body: dict):
    # httpx accepts a list of pairs, which is the only way to send the same header twice.
    pairs = [("Content-Type", "application/json"), ("Authorization", "Bearer t"), *header_list]
    return client.post("/mcp/orders", content=json.dumps(body), headers=pairs)


@pytest.mark.parametrize("dup", ["Mcp-Name", "Mcp-Method", "MCP-Protocol-Version"])
def test_repeated_routing_header_is_mismatch_before_authorize(dup):
    recorder = Recorder()
    client = TestClient(make_app(recorder))
    base = {"MCP-Protocol-Version": mcp.PROTOCOL_VERSION, "Mcp-Method": "tools/call", "Mcp-Name": "get_order_status"}
    pairs = list(base.items()) + [(dup, base[dup])]          # same value twice still counts as repeated
    if dup == "Mcp-Name":
        pairs = list(base.items()) + [("Mcp-Name", "get_order_details")]   # allowed first, forbidden second
    resp = _raw_post(client, pairs, call_body("get_order_status"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == mcp.HEADER_MISMATCH
    assert resp.json()["error"]["data"]["header"].lower() == dup.lower()
    assert recorder.authorize_calls == [] and recorder.handler_calls == []


def test_tools_call_without_name_anywhere_is_mismatch_before_authorize():
    recorder = Recorder()
    client = TestClient(make_app(recorder))
    body = rpc("tools/call", {"arguments": {"order_id": "CO-1042"}})       # no params.name
    resp = client.post("/mcp/orders", json=body, headers=headers("tools/call"))  # no Mcp-Name
    assert resp.status_code == 400
    assert resp.json()["error"] == {"code": mcp.HEADER_MISMATCH, "message": "HeaderMismatch",
                                    "data": {"header": "Mcp-Name"}}
    assert recorder.authorize_calls == []


def test_tools_call_with_header_but_no_body_name_is_mismatch():
    recorder = Recorder()
    client = TestClient(make_app(recorder))
    body = rpc("tools/call", {"arguments": {"order_id": "CO-1042"}})
    resp = client.post("/mcp/orders", json=body, headers=headers("tools/call", "get_order_status"))
    assert resp.status_code == 400 and resp.json()["error"]["code"] == mcp.HEADER_MISMATCH
    assert recorder.authorize_calls == []
