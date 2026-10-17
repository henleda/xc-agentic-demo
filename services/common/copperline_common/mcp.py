"""Minimal MCP 2026-07-28 stateless Streamable HTTP: a server router and a client.

The server check order is in services/common/README.md and is load-bearing for scenario S1b:
a header/body mismatch is rejected before the authorize hook or any tool code runs.
"""

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from copperline_common.jwt import bearer_token, unverified_claims
from copperline_common.logging import log_event
from copperline_common.outbound import HttpResult, send
from copperline_common.requestid import current_request_id
from copperline_common.tool_schemas import ToolDef, validate_args

PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = ("2026-07-28",)

HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
UNAUTHORIZED = -32001
FORBIDDEN = -32003
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

HEADER_PROTOCOL_VERSION = "MCP-Protocol-Version"
HEADER_METHOD = "Mcp-Method"
HEADER_NAME = "Mcp-Name"

METHOD_DISCOVER = "server/discover"
METHOD_LIST_TOOLS = "tools/list"
METHOD_CALL_TOOL = "tools/call"
KNOWN_METHODS = (METHOD_DISCOVER, METHOD_LIST_TOOLS, METHOD_CALL_TOOL)

CACHE_TTL_MS = 60000
CALL_TIMEOUT_S = 10.0


class ToolError(Exception):
    """Tool-level failure (e.g. "not your order"). Becomes HTTP 200 with isError: true."""


class UpstreamError(Exception):
    """Backend failure (e.g. ERP down or blocked). Becomes HTTP 502, JSON-RPC -32603."""

    def __init__(self, message: str, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status


class McpDenied(Exception):
    """Raised by an authorize hook, e.g. McpDenied(403, FORBIDDEN, "Forbidden")."""

    def __init__(self, http_status: int, code: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message


@dataclass
class McpRequestContext:
    method: str
    tool: str | None
    token: str | None
    claims: dict
    request_id: str


ToolHandler = Callable[[dict, McpRequestContext], Awaitable[dict | list | str]]
AuthorizeHook = Callable[[McpRequestContext], Awaitable[None]]


def jsonrpc_error(id, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": error}


# ---------------------------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------------------------


@dataclass
class _Reject:
    http_status: int
    code: int
    message: str
    data: Any = None


def _params_of(body: dict) -> dict:
    params = body.get("params")
    return params if isinstance(params, dict) else {}


def _reject_constant(name: str):
    """NaN, Infinity and -Infinity are not JSON (RFC 8259); Python's parser accepts them unless told otherwise."""
    raise ValueError(f"invalid JSON constant {name}")


def _check_envelope(raw: bytes) -> tuple[dict | None, _Reject | None]:
    """Check 0: a JSON object with "jsonrpc": "2.0" and a string method."""
    try:
        body = json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        return None, _Reject(400, PARSE_ERROR, "Parse error")
    if not isinstance(body, dict):
        return None, _Reject(400, INVALID_REQUEST, "Invalid Request")
    if body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return body, _Reject(400, INVALID_REQUEST, "Invalid Request")
    return body, None


def _check_headers(headers, body: dict) -> _Reject | None:
    """Checks 1-4. Header names are case-insensitive (Starlette Headers); values must match exactly."""
    params = _params_of(body)
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}

    # A repeated routing header is a mismatch: an LB and this server might each read a different copy.
    for name in (HEADER_PROTOCOL_VERSION, HEADER_METHOD, HEADER_NAME):
        if len(headers.getlist(name)) > 1:
            return _mismatch(name)

    version = headers.get(HEADER_PROTOCOL_VERSION)
    if version is None or (META_PROTOCOL_VERSION in meta and meta[META_PROTOCOL_VERSION] != version):
        return _mismatch(HEADER_PROTOCOL_VERSION)
    if headers.get(HEADER_METHOD) != body["method"]:
        return _mismatch(HEADER_METHOD)
    if body["method"] == METHOD_CALL_TOOL:
        # Both sides must be present and equal; "neither present" is not agreement.
        header_name, body_name = headers.get(HEADER_NAME), params.get("name")
        if header_name is None or body_name is None or header_name != body_name:
            return _mismatch(HEADER_NAME)
    if version not in SUPPORTED_VERSIONS:
        data = {"supported": list(SUPPORTED_VERSIONS), "requested": version}
        return _Reject(400, UNSUPPORTED_PROTOCOL_VERSION, "UnsupportedProtocolVersion", data)
    return None


def _mismatch(header: str) -> _Reject:
    return _Reject(400, HEADER_MISMATCH, "HeaderMismatch", {"header": header})


def _tool_result(value: dict | list | str) -> dict:
    structured = value if isinstance(value, dict) else {"result": value}
    return {
        "content": [{"type": "text", "text": json.dumps(structured)}],
        "structuredContent": structured,
        "isError": False,
        "resultType": "complete",
    }


def _tool_error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True, "resultType": "complete"}


class _McpServer:
    def __init__(self, server_name, tools, handlers, authorize, server_version) -> None:
        self.server_name = server_name
        self.tools = {tool.name: tool for tool in tools}
        self.handlers = handlers
        self.authorize = authorize
        self.server_info = {"name": server_name, "version": server_version}
        missing = sorted(set(self.tools) - set(handlers))
        extra = sorted(set(handlers) - set(self.tools))
        if missing or extra:
            raise ValueError(
                f"mcp_router({server_name}): tools without handlers {missing}, handlers without tools {extra}"
            )

    async def handle_post(self, request: Request) -> JSONResponse:
        body, reject = _check_envelope(await request.body())
        request_id_in_body = body.get("id") if body else None
        if reject is None:
            reject = _check_headers(request.headers, body)
        if reject is not None:
            return self._reject(request_id_in_body, reject, body)

        params = _params_of(body)
        method = body["method"]
        tool_name = params.get("name") if method == METHOD_CALL_TOOL else None
        token = bearer_token(request.headers.get("authorization"))
        ctx = McpRequestContext(
            method=method,
            tool=tool_name if isinstance(tool_name, str) else None,
            token=token,
            claims=unverified_claims(token) if token else {},
            request_id=current_request_id(),
        )
        if self.authorize is not None:
            try:
                await self.authorize(ctx)
            except McpDenied as denied:
                return self._reject(body.get("id"), _Reject(denied.http_status, denied.code, denied.message), body)

        if method == METHOD_DISCOVER:
            return self._result(body, self._discover_result())
        if method == METHOD_LIST_TOOLS:
            return self._result(body, self._list_result())
        if method == METHOD_CALL_TOOL:
            return await self._call_tool(body, params, ctx)
        reject = _Reject(404, METHOD_NOT_FOUND, "Method not found", {"method": method})
        return self._reject(body.get("id"), reject, body)

    def _discover_result(self) -> dict:
        return {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "resultType": "complete",
            "ttlMs": CACHE_TTL_MS,
            "cacheScope": "public",
            "_meta": {META_SERVER_INFO: self.server_info},
        }

    def _list_result(self) -> dict:
        tools = [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
            for tool in self.tools.values()
        ]
        return {
            "tools": tools,
            "resultType": "complete",
            "ttlMs": CACHE_TTL_MS,
            "cacheScope": "public",
            "_meta": {META_SERVER_INFO: self.server_info},
        }

    async def _call_tool(self, body: dict, params: dict, ctx: McpRequestContext) -> JSONResponse:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            reject = _Reject(400, INVALID_PARAMS, "Invalid params", {"tool": name, "detail": "unknown tool"})
            return self._reject(body.get("id"), reject, body)

        started_at = time.monotonic()
        arguments = params.get("arguments")
        arguments = {} if arguments is None else arguments
        problem = validate_args(tool, arguments)
        if problem is not None:
            return self._tool_response(body, tool, _tool_error_result(problem), started_at, error=problem)
        try:
            value = await self.handlers[tool.name](arguments, ctx)
        except ToolError as e:
            return self._tool_response(body, tool, _tool_error_result(str(e)), started_at, error=str(e))
        except UpstreamError as e:
            data = {"upstream_status": e.upstream_status}
            self._log_tool_call(tool, 502, None, started_at, error=str(e))
            return JSONResponse(jsonrpc_error(body.get("id"), INTERNAL_ERROR, "Internal error", data), status_code=502)
        return self._tool_response(body, tool, _tool_result(value), started_at)

    def _tool_response(self, body, tool, result, started_at, error=None) -> JSONResponse:
        self._log_tool_call(tool, 200, result["isError"], started_at, error=error)
        return self._result(body, result)

    def _log_tool_call(self, tool: ToolDef, status: int, is_error: bool | None, started_at: float, error=None):
        latency_ms = int((time.monotonic() - started_at) * 1000)
        log_event(
            "tool_call",
            server=self.server_name,
            tool=tool.name,
            status=status,
            is_error=is_error,
            latency_ms=latency_ms,
            error=error,
        )

    def _result(self, body: dict, result: dict) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    def _reject(self, id, reject: _Reject, body: dict | None) -> JSONResponse:
        params = _params_of(body) if body else {}
        log_event(
            "mcp_reject",
            level="warning",
            server=self.server_name,
            method=body.get("method") if body else None,
            tool=params.get("name"),
            status=reject.http_status,
            code=reject.code,
            message=reject.message,
            data=reject.data,
        )
        return JSONResponse(jsonrpc_error(id, reject.code, reject.message, reject.data), status_code=reject.http_status)


async def _method_not_allowed() -> JSONResponse:
    body = jsonrpc_error(None, INVALID_REQUEST, "Method Not Allowed: use POST")
    return JSONResponse(body, status_code=405, headers={"Allow": "POST"})


def mcp_router(
    *,
    server_name: str,
    path: str,
    tools: list[ToolDef],
    handlers: dict[str, ToolHandler],
    authorize: AuthorizeHook | None = None,
    server_version: str = "1.0",
) -> APIRouter:
    """POST <path> speaks MCP; GET and DELETE return 405. handlers are keyed by short tool name."""
    server = _McpServer(server_name, tools, handlers, authorize, server_version)
    router = APIRouter()
    router.add_api_route(path, server.handle_post, methods=["POST"], include_in_schema=False)
    router.add_api_route(path, _method_not_allowed, methods=["GET", "DELETE"], include_in_schema=False)
    return router


# ---------------------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------------------


class McpClient:
    """Sends MCP requests and returns the raw HttpResult. Never raises on HTTP status."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        client_name: str = "copperline-agent",
        client_version: str = "1.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.client_info = {"name": client_name, "version": client_version}

    def url(self, server: str) -> str:
        return f"{self.base_url}/mcp/{server}"

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict,
        *,
        token: str | None,
        header_name_override: str | None = None,
        extra_headers: dict | None = None,
    ) -> HttpResult:
        """header_name_override sends a different Mcp-Name than the body tool name (scenario S1b only)."""
        params = {"name": tool, "arguments": arguments}
        name_header = header_name_override if header_name_override is not None else tool
        return await self._post(server, METHOD_CALL_TOOL, params, token, name_header, extra_headers)

    async def list_tools(self, server: str, *, token: str | None) -> HttpResult:
        return await self._post(server, METHOD_LIST_TOOLS, {}, token)

    async def discover(self, server: str, *, token: str | None) -> HttpResult:
        return await self._post(server, METHOD_DISCOVER, {}, token)

    async def _post(self, server, method, params, token, name_header=None, extra_headers=None) -> HttpResult:
        meta = {
            META_PROTOCOL_VERSION: PROTOCOL_VERSION,
            META_CLIENT_CAPABILITIES: {},
            META_CLIENT_INFO: self.client_info,
        }
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": {**params, "_meta": meta}}
        # Protocol headers are set last so extra_headers can never change them by accident.
        headers = httpx.Headers(extra_headers or {})
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        headers[HEADER_PROTOCOL_VERSION] = PROTOCOL_VERSION
        headers[HEADER_METHOD] = method
        if name_header is not None:
            headers[HEADER_NAME] = name_header
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return await send(
            self.http,
            "POST",
            self.url(server),
            json=body,
            headers=headers,
            timeout=CALL_TIMEOUT_S,
            retry_connect_once=True,
        )


def _jsonrpc_result(result: HttpResult) -> dict | None:
    if isinstance(result.json, dict) and isinstance(result.json.get("result"), dict):
        return result.json["result"]
    return None


def tool_result_text(result: HttpResult) -> str | None:
    """content[0].text of a tools/call result, else None."""
    content = (_jsonrpc_result(result) or {}).get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = content[0].get("text")
        return text if isinstance(text, str) else None
    return None


def tool_result_is_error(result: HttpResult) -> bool | None:
    """result.isError for a JSON-RPC result (absent counts as False), else None."""
    rpc_result = _jsonrpc_result(result)
    if rpc_result is None:
        return None
    return bool(rpc_result.get("isError", False))


def jsonrpc_error_of(result: HttpResult) -> dict | None:
    if isinstance(result.json, dict) and isinstance(result.json.get("error"), dict):
        return result.json["error"]
    return None
