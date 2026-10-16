"""Black-box contract tests for mcp.mcp_router: the server check order and result shapes.

Source of truth: services/common/README.md ("Minimal MCP 2026-07-28") and API.md (mcp.py).
The orders tool server is wired with recording authorize/handler hooks so a test can prove
that "nothing after a failed check runs".
"""
import json

import pytest
from copperline_common.mcp import (
    FORBIDDEN,
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    META_CLIENT_CAPABILITIES,
    META_CLIENT_INFO,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    UNAUTHORIZED,
    UNSUPPORTED_PROTOCOL_VERSION,
    McpDenied,
    ToolError,
    UpstreamError,
    mcp_router,
)
from copperline_common.tool_schemas import ToolDef
from fastapi import FastAPI
from fastapi.testclient import TestClient

OLD_VERSION = "2025-06-18"
MCP_PATH = "/mcp/orders"


def order_tool(name, description, arg):
    schema = {"type": "object", "properties": {arg: {"type": "string"}}, "required": [arg],
              "additionalProperties": False}
    return ToolDef(server="orders", name=name, description=description, input_schema=schema)


ORDERS_TOOLS = [
    order_tool("get_order_status", "Order status (masked; no PII)", "order_id"),
    order_tool("get_order_details", "Full order incl. payment and contact (PII)", "order_id"),
    order_tool("list_customer_orders", "All orders for a customer", "customer_id"),
]


class OrdersServer:
    """An orders MCP server whose hooks record every invocation."""

    def __init__(self):
        self.authorize_calls = []
        self.handler_calls = []
        handlers = {
            "get_order_status": self.get_order_status,
            "get_order_details": self.get_order_details,
            "list_customer_orders": self.list_customer_orders,
        }
        router = mcp_router(server_name="orders", path=MCP_PATH, tools=ORDERS_TOOLS, handlers=handlers,
                            authorize=self.authorize, server_version="2.5")
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    async def authorize(self, ctx):
        self.authorize_calls.append(ctx)
        if ctx.token is None:
            raise McpDenied(401, UNAUTHORIZED, "Unauthorized")
        if (ctx.tool or "").endswith("get_order_details"):
            raise McpDenied(403, FORBIDDEN, "Forbidden")

    async def get_order_status(self, args, ctx):
        self.handler_calls.append(("get_order_status", args, ctx))
        if args["order_id"] == "CO-9999":
            raise ToolError("not your order")
        if args["order_id"] == "CO-0503":
            raise UpstreamError("ERP unavailable", upstream_status=503)
        return {"order_id": args["order_id"], "status": "shipped"}

    async def get_order_details(self, args, ctx):
        self.handler_calls.append(("get_order_details", args, ctx))
        return {"order_id": args["order_id"], "card": "4111111111111111"}

    async def list_customer_orders(self, args, ctx):
        self.handler_calls.append(("list_customer_orders", args, ctx))
        return [{"order_id": "CO-1042"}, {"order_id": "CO-1077"}]

    def post(self, body, headers):
        raw = body if isinstance(body, bytes) else json.dumps(body)
        return self.client.post(MCP_PATH, content=raw, headers=headers)


@pytest.fixture
def server():
    return OrdersServer()


def meta(version=PROTOCOL_VERSION):
    return {
        META_PROTOCOL_VERSION: version,
        META_CLIENT_CAPABILITIES: {},
        META_CLIENT_INFO: {"name": "contract-test", "version": "0"},
    }


def rpc(method, params=None, *, req_id="req-1"):
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params if params is not None else {}}


def call_body(name, arguments, *, version=PROTOCOL_VERSION):
    return rpc("tools/call", {"name": name, "arguments": arguments, "_meta": meta(version)})


def headers(method, name=None, *, version=PROTOCOL_VERSION, token="agent-token"):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if method is not None:
        h["Mcp-Method"] = method
    if version is not None:
        h["MCP-Protocol-Version"] = version
    if name is not None:
        h["Mcp-Name"] = name
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return h


def good_call(name, arguments):
    """A well-formed tools/call: body and headers agree."""
    return call_body(name, arguments), headers("tools/call", name)


def assert_json(resp):
    content_type = resp.headers["content-type"]
    assert content_type.startswith("application/json"), content_type
    assert "text/event-stream" not in content_type


def assert_error(resp, http_status, code, *, req_id="req-1"):
    assert resp.status_code == http_status, resp.text
    assert_json(resp)
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == req_id
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    return body["error"]


def assert_tool_result(resp, *, is_error):
    assert resp.status_code == 200, resp.text
    assert_json(resp)
    result = resp.json()["result"]
    assert result["isError"] is is_error
    assert result["resultType"] == "complete"
    assert result["content"][0]["type"] == "text"
    assert isinstance(result["content"][0]["text"], str)
    return result


# --- check 0: body shape -------------------------------------------------------------------------


def test_body_that_is_not_json_is_parse_error(server):
    resp = server.post(b"{not json", headers("tools/list"))
    assert_error(resp, 400, PARSE_ERROR, req_id=None)


@pytest.mark.parametrize("body", [
    {"id": "req-1", "method": "tools/list", "params": {}},               # no jsonrpc
    {"jsonrpc": "1.0", "id": "req-1", "method": "tools/list"},           # wrong jsonrpc
    {"jsonrpc": "2.0", "id": "req-1", "method": 7},                      # method not a string
])
def test_body_that_is_not_a_jsonrpc_request_is_invalid_request(server, body):
    resp = server.post(body, headers("tools/list"))
    assert resp.status_code == 400
    assert_json(resp)
    assert resp.json()["error"]["code"] == INVALID_REQUEST


def test_json_array_body_is_invalid_request(server):
    resp = server.post([rpc("tools/list")], headers("tools/list"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == INVALID_REQUEST


# --- check 1: MCP-Protocol-Version -----------------------------------------------------------------


def test_missing_protocol_version_header_is_header_mismatch(server):
    resp = server.post(rpc("tools/list", {"_meta": meta()}), headers("tools/list", version=None))
    error = assert_error(resp, 400, HEADER_MISMATCH)
    assert error["data"]["header"] == "MCP-Protocol-Version"
    assert server.authorize_calls == []


def test_protocol_version_header_differing_from_meta_is_header_mismatch(server):
    body = call_body("get_order_status", {"order_id": "CO-1042"}, version=OLD_VERSION)
    resp = server.post(body, headers("tools/call", "get_order_status"))
    error = assert_error(resp, 400, HEADER_MISMATCH)
    assert error["data"]["header"] == "MCP-Protocol-Version"
    assert server.authorize_calls == [] and server.handler_calls == []


def test_body_without_meta_only_needs_a_valid_header(server):
    resp = server.post(rpc("tools/list", {}), headers("tools/list"))
    assert resp.status_code == 200, resp.text
    assert "tools" in resp.json()["result"]


# --- check 2: Mcp-Method ---------------------------------------------------------------------------


@pytest.mark.parametrize("header_method", ["tools/list", "server/discover", None])
def test_mcp_method_header_not_equal_to_body_method_is_header_mismatch(server, header_method):
    body = call_body("get_order_status", {"order_id": "CO-1042"})
    resp = server.post(body, headers(header_method, "get_order_status"))
    error = assert_error(resp, 400, HEADER_MISMATCH)
    assert error["data"]["header"] == "Mcp-Method"
    assert server.authorize_calls == [] and server.handler_calls == []


# --- check 3: Mcp-Name (S1b) -----------------------------------------------------------------------


def test_mcp_name_mismatch_is_rejected_before_authorize_and_handler(server):
    """S1b: header says the allowed tool, body says the forbidden one. Nothing after check 3 may run."""
    body = call_body("get_order_details", {"order_id": "CO-1042"})
    resp = server.post(body, headers("tools/call", "get_order_status"))
    error = assert_error(resp, 400, HEADER_MISMATCH)
    assert error["data"]["header"] == "Mcp-Name"
    assert server.authorize_calls == []
    assert server.handler_calls == []


def test_missing_mcp_name_on_tools_call_is_header_mismatch(server):
    body = call_body("get_order_status", {"order_id": "CO-1042"})
    resp = server.post(body, headers("tools/call", None))
    error = assert_error(resp, 400, HEADER_MISMATCH)
    assert error["data"]["header"] == "Mcp-Name"
    assert server.handler_calls == []


def test_mcp_name_comparison_is_exact_on_the_value(server):
    body = call_body("get_order_status", {"order_id": "CO-1042"})
    resp = server.post(body, headers("tools/call", "Get_Order_Status"))
    error = assert_error(resp, 400, HEADER_MISMATCH)
    assert error["data"]["header"] == "Mcp-Name"


def test_header_names_are_case_insensitive(server):
    hdrs = {
        "content-type": "application/json",
        "mcp-protocol-version": PROTOCOL_VERSION,
        "MCP-METHOD": "tools/call",
        "mcp-name": "get_order_status",
        "authorization": "Bearer agent-token",
    }
    resp = server.post(call_body("get_order_status", {"order_id": "CO-1042"}), hdrs)
    assert_tool_result(resp, is_error=False)


def test_header_mismatch_is_reported_before_unsupported_version(server):
    body = call_body("get_order_details", {"order_id": "CO-1042"}, version=OLD_VERSION)
    resp = server.post(body, headers("tools/call", "get_order_status", version=OLD_VERSION))
    error = assert_error(resp, 400, HEADER_MISMATCH)
    assert error["data"]["header"] == "Mcp-Name"


# --- check 4: supported version --------------------------------------------------------------------


def test_unsupported_protocol_version_is_rejected(server):
    body = call_body("get_order_status", {"order_id": "CO-1042"}, version=OLD_VERSION)
    resp = server.post(body, headers("tools/call", "get_order_status", version=OLD_VERSION))
    error = assert_error(resp, 400, UNSUPPORTED_PROTOCOL_VERSION)
    assert PROTOCOL_VERSION in error["data"]["supported"]
    assert error["data"]["requested"] == OLD_VERSION
    assert server.authorize_calls == [] and server.handler_calls == []


# --- check 5: authorize hook -----------------------------------------------------------------------


def test_authorize_denial_maps_to_its_http_status_and_code(server):
    body = call_body("get_order_details", {"order_id": "CO-1042"})
    resp = server.post(body, headers("tools/call", "get_order_details"))
    assert_error(resp, 403, FORBIDDEN)
    assert len(server.authorize_calls) == 1
    assert server.handler_calls == []


def test_authorize_unauthorized_maps_to_401(server):
    body = call_body("get_order_status", {"order_id": "CO-1042"})
    resp = server.post(body, headers("tools/call", "get_order_status", token=None))
    assert_error(resp, 401, UNAUTHORIZED)
    assert server.handler_calls == []


def test_authorize_receives_method_tool_and_bearer_token(server):
    server.post(*good_call("get_order_status", {"order_id": "CO-1042"}))
    (ctx,) = server.authorize_calls
    assert ctx.method == "tools/call"
    assert ctx.tool.endswith("get_order_status")
    assert ctx.token == "agent-token"
    assert isinstance(ctx.claims, dict)
    assert isinstance(ctx.request_id, str) and ctx.request_id


# --- checks 6-10 -----------------------------------------------------------------------------------


def test_unknown_method_is_method_not_found_404(server):
    resp = server.post(rpc("resources/list", {"_meta": meta()}), headers("resources/list"))
    assert_error(resp, 404, METHOD_NOT_FOUND)


def test_unknown_tool_is_invalid_params(server):
    body = call_body("issue_refund", {"order_id": "CO-1042", "amount": 1})
    resp = server.post(body, headers("tools/call", "issue_refund"))
    assert_error(resp, 400, INVALID_PARAMS)
    assert server.handler_calls == []


@pytest.mark.parametrize("arguments", [
    {},                                          # missing required order_id
    {"order_id": 1042},                          # wrong type
    {"order_id": "CO-1042", "verbose": True},    # unknown key (additionalProperties: false)
])
def test_arguments_not_matching_schema_return_is_error_result(server, arguments):
    resp = server.post(call_body("get_order_status", arguments), headers("tools/call", "get_order_status"))
    result = assert_tool_result(resp, is_error=True)
    assert result["content"][0]["text"]
    assert server.handler_calls == []


def test_tool_error_returns_200_with_is_error_and_message(server):
    resp = server.post(*good_call("get_order_status", {"order_id": "CO-9999"}))
    result = assert_tool_result(resp, is_error=True)
    assert "not your order" in result["content"][0]["text"]


def test_upstream_error_is_502_internal_error_with_upstream_status(server):
    resp = server.post(*good_call("get_order_status", {"order_id": "CO-0503"}))
    error = assert_error(resp, 502, INTERNAL_ERROR)
    assert error["data"]["upstream_status"] == 503


@pytest.mark.parametrize("verb", ["GET", "DELETE"])
def test_get_and_delete_on_the_mcp_path_are_405(server, verb):
    resp = server.client.request(verb, MCP_PATH, headers=headers("tools/list"))
    assert resp.status_code == 405
    assert_json(resp)


@pytest.mark.parametrize("case", ["ok", "header_mismatch", "forbidden", "unknown_method", "tool_error", "upstream"])
def test_every_response_is_application_json_never_sse(server, case):
    requests = {
        "ok": good_call("get_order_status", {"order_id": "CO-1"}),
        "header_mismatch": (call_body("get_order_details", {"order_id": "CO-1"}),
                            headers("tools/call", "get_order_status")),
        "forbidden": good_call("get_order_details", {"order_id": "CO-1"}),
        "unknown_method": (rpc("prompts/list"), headers("prompts/list")),
        "tool_error": good_call("get_order_status", {"order_id": "CO-9999"}),
        "upstream": good_call("get_order_status", {"order_id": "CO-0503"}),
    }
    body, hdrs = requests[case]
    assert_json(server.post(body, hdrs))


# --- result shapes ---------------------------------------------------------------------------------


def test_server_discover_result_shape(server):
    resp = server.post(rpc("server/discover", {"_meta": meta()}), headers("server/discover"))
    assert resp.status_code == 200
    assert_json(resp)
    body = resp.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == "req-1"
    result = body["result"]
    assert result["supportedVersions"] == [PROTOCOL_VERSION]
    assert result["capabilities"] == {"tools": {}}
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 60000
    assert result["cacheScope"] == "public"
    assert result["_meta"][META_SERVER_INFO] == {"name": "orders", "version": "2.5"}


def test_tools_list_result_shape(server):
    resp = server.post(rpc("tools/list", {"_meta": meta()}), headers("tools/list"))
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 60000
    assert result["cacheScope"] == "public"
    assert result["_meta"][META_SERVER_INFO]["name"] == "orders"
    by_name = {t["name"]: t for t in result["tools"]}
    assert set(by_name) == {"get_order_status", "get_order_details", "list_customer_orders"}
    for tool in by_name.values():
        assert set(tool) >= {"name", "description", "inputSchema"}
    assert by_name["get_order_status"]["inputSchema"] == {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
        "additionalProperties": False,
    }


def test_tools_call_success_shape(server):
    resp = server.post(*good_call("get_order_status", {"order_id": "CO-1042"}))
    result = assert_tool_result(resp, is_error=False)
    assert resp.json()["id"] == "req-1"
    assert result["structuredContent"] == {"order_id": "CO-1042", "status": "shipped"}
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_list_return_value_is_wrapped_as_result(server):
    body = call_body("list_customer_orders", {"customer_id": "C-1001"})
    resp = server.post(body, headers("tools/call", "list_customer_orders"))
    result = assert_tool_result(resp, is_error=False)
    assert result["structuredContent"] == {"result": [{"order_id": "CO-1042"}, {"order_id": "CO-1077"}]}
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_handler_receives_arguments_and_context(server):
    server.post(*good_call("get_order_status", {"order_id": "CO-1042"}))
    ((name, args, ctx),) = server.handler_calls
    assert name == "get_order_status"
    assert args == {"order_id": "CO-1042"}
    assert ctx.method == "tools/call"
    assert ctx.token == "agent-token"
