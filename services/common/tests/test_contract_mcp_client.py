"""Black-box contract tests for mcp.McpClient and the result helpers (README "Client rules").

All HTTP goes through httpx.MockTransport; the transport counts calls so retry rules can be checked.
"""
import asyncio
import json

import httpx
import pytest
from copperline_common.mcp import (
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    McpClient,
    jsonrpc_error_of,
    tool_result_is_error,
    tool_result_text,
)
from copperline_common.outbound import HttpResult, new_http_client

BASE = "http://tools.test"


class Recorder:
    """MockTransport handler: records requests; `script` is a list of responses or exceptions to replay."""

    def __init__(self, script=None):
        self.requests = []
        self.script = list(script or [])

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        item = self.script.pop(0) if self.script else ok_response()
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, index=-1):
        return json.loads(self.requests[index].content)

    def headers(self, index=-1):
        return self.requests[index].headers


def ok_response(status=200, payload=None):
    if payload is None:
        payload = {"jsonrpc": "2.0", "id": "x", "result": {
            "content": [{"type": "text", "text": "{\"status\": \"shipped\"}"}],
            "structuredContent": {"status": "shipped"}, "isError": False, "resultType": "complete"}}
    return httpx.Response(status, json=payload, headers={"x-copperline-origin": "mcp-orders"})


def connect_error():
    return httpx.ConnectError("connection refused")


def run_client(recorder, action, *, use_common_client=False):
    """Run `action(client)` (a coroutine factory) against the recorder; returns the HttpResult."""

    async def go():
        transport = httpx.MockTransport(recorder)
        http = new_http_client(transport=transport) if use_common_client else httpx.AsyncClient(transport=transport)
        async with http:
            return await action(McpClient(BASE, http))

    return asyncio.run(go())


def call_status(client):
    return client.call_tool("orders", "get_order_status", {"order_id": "CO-1042"}, token="tok-123")


# --- request shape ---------------------------------------------------------------------------------


def test_url_is_base_plus_mcp_server():
    client = McpClient(BASE, httpx.AsyncClient())
    assert client.url("orders") == "http://tools.test/mcp/orders"


def test_call_tool_sends_protocol_headers_and_meta():
    rec = Recorder()
    run_client(rec, call_status)
    (request,) = rec.requests
    assert request.method == "POST"
    assert str(request.url) == "http://tools.test/mcp/orders"
    h = request.headers
    assert h["MCP-Protocol-Version"] == PROTOCOL_VERSION == "2026-07-28"
    assert h["Mcp-Method"] == "tools/call"
    assert h["Mcp-Name"] == "get_order_status"
    assert h["Authorization"] == "Bearer tok-123"
    assert h["Content-Type"].startswith("application/json")
    assert "application/json" in h["Accept"]

    body = rec.body()
    assert body["jsonrpc"] == "2.0"
    assert isinstance(body["id"], str | int) and body["id"] not in ("", None)
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "get_order_status"
    assert body["params"]["arguments"] == {"order_id": "CO-1042"}
    meta = body["params"]["_meta"]
    assert meta[META_PROTOCOL_VERSION] == PROTOCOL_VERSION
    assert meta[META_CLIENT_CAPABILITIES] == {}
    assert meta[META_CLIENT_INFO] == {"name": "copperline-agent", "version": "1.0"}


def test_client_name_and_version_go_into_client_info():
    rec = Recorder()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as http:
            client = McpClient(BASE, http, client_name="cockpit", client_version="9.9")
            return await client.list_tools("catalog", token=None)

    asyncio.run(go())
    assert rec.body()["params"]["_meta"][META_CLIENT_INFO] == {"name": "cockpit", "version": "9.9"}


def test_header_name_override_sends_a_different_mcp_name_than_the_body():
    rec = Recorder()
    run_client(rec, lambda c: c.call_tool("orders", "get_order_details", {"order_id": "CO-1042"}, token="t",
                                          header_name_override="get_order_status"))
    assert rec.headers()["Mcp-Name"] == "get_order_status"
    assert rec.body()["params"]["name"] == "get_order_details"
    assert rec.headers()["Mcp-Method"] == rec.body()["method"] == "tools/call"


def test_extra_headers_are_added():
    rec = Recorder()
    run_client(rec, lambda c: c.call_tool("orders", "get_order_status", {"order_id": "CO-1"}, token="t",
                                          extra_headers={"x-agent-id": "shopper", "x-obo-sub": "alex.rivera"}))
    assert rec.headers()["x-agent-id"] == "shopper"
    assert rec.headers()["x-obo-sub"] == "alex.rivera"


def test_list_tools_sends_method_headers_and_meta():
    rec = Recorder()
    run_client(rec, lambda c: c.list_tools("catalog", token="tok"))
    h = rec.headers()
    assert str(rec.requests[0].url) == "http://tools.test/mcp/catalog"
    assert h["Mcp-Method"] == "tools/list"
    assert h["MCP-Protocol-Version"] == PROTOCOL_VERSION
    assert "Mcp-Name" not in h
    body = rec.body()
    assert body["method"] == "tools/list"
    assert body["params"]["_meta"][META_PROTOCOL_VERSION] == PROTOCOL_VERSION


def test_discover_sends_method_headers_and_meta():
    rec = Recorder()
    run_client(rec, lambda c: c.discover("refunds", token="tok"))
    h = rec.headers()
    assert h["Mcp-Method"] == "server/discover"
    assert h["MCP-Protocol-Version"] == PROTOCOL_VERSION
    assert rec.body()["method"] == "server/discover"
    assert META_CLIENT_INFO in rec.body()["params"]["_meta"]


def test_no_token_means_no_authorization_header():
    rec = Recorder()
    run_client(rec, lambda c: c.list_tools("catalog", token=None))
    assert "Authorization" not in rec.headers()


def test_request_id_header_is_propagated_with_the_common_http_client():
    rec = Recorder()
    run_client(rec, call_status, use_common_client=True)
    assert rec.headers().get("x-request-id")


# --- results and retries ---------------------------------------------------------------------------


def test_success_returns_raw_http_result():
    rec = Recorder()
    result = run_client(rec, call_status)
    assert isinstance(result, HttpResult)
    assert result.status == 200
    assert result.headers["x-copperline-origin"] == "mcp-orders"
    assert result.json["result"]["structuredContent"] == {"status": "shipped"}
    assert result.error is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
def test_no_retry_on_any_http_status(status):
    rec = Recorder([httpx.Response(status, text="denied")] * 3)
    result = run_client(rec, call_status)
    assert len(rec.requests) == 1
    assert result.status == status
    assert result.body_text == "denied"
    assert result.json is None


def test_exactly_one_retry_on_connect_error_then_success():
    rec = Recorder([connect_error(), ok_response()])
    result = run_client(rec, call_status)
    assert len(rec.requests) == 2
    assert result.status == 200


def test_connect_error_twice_gives_up_after_one_retry():
    rec = Recorder([connect_error(), connect_error(), ok_response()])
    result = run_client(rec, call_status)
    assert len(rec.requests) == 2
    assert result.status is None
    assert result.error


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), httpx.WriteTimeout("slow")])
def test_no_retry_on_timeout(exc):
    rec = Recorder([exc, ok_response()])
    result = run_client(rec, call_status)
    assert len(rec.requests) == 1
    assert result.status is None
    assert "timeout" in result.error


# --- helpers over HttpResult -----------------------------------------------------------------------


def http_result(status, payload):
    text = json.dumps(payload) if payload is not None else "<html>blocked</html>"
    return HttpResult(status=status, headers={}, body_text=text, json=payload, latency_ms=3)


def test_tool_result_helpers_on_success():
    result = http_result(200, {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": "{\"a\": 1}"}], "structuredContent": {"a": 1},
        "isError": False, "resultType": "complete"}})
    assert tool_result_text(result) == "{\"a\": 1}"
    assert tool_result_is_error(result) is False
    assert jsonrpc_error_of(result) is None


def test_tool_result_helpers_on_tool_error():
    result = http_result(200, {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": "not your order"}], "isError": True, "resultType": "complete"}})
    assert tool_result_text(result) == "not your order"
    assert tool_result_is_error(result) is True


def test_tool_result_helpers_on_jsonrpc_error():
    error = {"code": -32020, "message": "HeaderMismatch", "data": {"header": "Mcp-Name"}}
    result = http_result(400, {"jsonrpc": "2.0", "id": 1, "error": error})
    assert jsonrpc_error_of(result) == error
    assert tool_result_text(result) is None
    assert tool_result_is_error(result) is None


def test_tool_result_helpers_on_non_json_block_page():
    result = http_result(403, None)
    assert tool_result_text(result) is None
    assert tool_result_is_error(result) is None
    assert jsonrpc_error_of(result) is None
