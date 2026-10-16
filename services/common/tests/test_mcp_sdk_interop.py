"""Dev-only: the official MCP Python SDK client can discover, list and call one of our mcp_router servers.

This is the only test that opens a socket (localhost only). It is skipped when the `mcp` package is absent.
"""

import asyncio
import json
import socket
import threading
import time

import pytest

mcp_sdk = pytest.importorskip("mcp")
httpx2 = pytest.importorskip("httpx2")
uvicorn = pytest.importorskip("uvicorn")

from copperline_common import mcp  # noqa: E402
from copperline_common.service import setup_service  # noqa: E402
from copperline_common.tool_schemas import ToolDef, shorthand_to_schema  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

pytestmark = pytest.mark.interop

TOKEN = "interop-test-token"
TOOLS = [
    ToolDef("catalog", "get_product", "Get one product", shorthand_to_schema({"product_id": "string"})),
    ToolDef("catalog", "search_products", "Search", shorthand_to_schema({"query": "string", "limit": "integer?"})),
]


async def _authorize(ctx: mcp.McpRequestContext) -> None:
    if ctx.token != TOKEN:
        raise mcp.McpDenied(401, mcp.UNAUTHORIZED, "Unauthorized")


async def _get_product(args: dict, ctx: mcp.McpRequestContext) -> dict:
    if args["product_id"] == "MISSING":
        raise mcp.ToolError("no such product")
    return {"product_id": args["product_id"], "name": "Alpine 2 tent"}


async def _search(args: dict, ctx: mcp.McpRequestContext) -> list:
    return [args["query"]] * args.get("limit", 1)


def _app() -> FastAPI:
    app = FastAPI()
    setup_service(app, "mcp-catalog")
    handlers = {"get_product": _get_product, "search_products": _search}
    app.include_router(
        mcp.mcp_router(server_name="catalog", path="/mcp/catalog", tools=TOOLS, handlers=handlers, authorize=_authorize)
    )
    return app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    port = _free_port()
    config = uvicorn.Config(_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("uvicorn did not start")
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp/catalog"
    server.should_exit = True
    thread.join(timeout=5)


def _client(url: str, token: str | None = TOKEN, mode: str = "auto"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx2.AsyncClient(headers=headers, timeout=10.0)
    transport = streamable_http_client(url, http_client=http)
    return mcp_sdk.Client(transport, mode=mode), http


def _mcp_error(exc: BaseException) -> MCPError | None:
    """The SDK raises from inside anyio task groups, so the MCPError may be nested in ExceptionGroups."""
    if isinstance(exc, MCPError):
        return exc
    for inner in getattr(exc, "exceptions", ()):
        found = _mcp_error(inner)
        if found is not None:
            return found
    return None


def _raised_mcp_error(coro_factory) -> MCPError:
    try:
        asyncio.run(coro_factory())
    except BaseException as exc:  # noqa: BLE001 - we inspect it below
        found = _mcp_error(exc)
        if found is None:
            raise
        return found
    pytest.fail("expected an MCPError")


def test_sdk_client_discovers_lists_and_calls(server_url):
    async def go():
        client, http = _client(server_url)
        async with http, client:
            listed = await client.list_tools()
            ok = await client.call_tool("get_product", {"product_id": "TNT-ALPINE-2"})
            wrapped = await client.call_tool("search_products", {"query": "tent", "limit": 2})
            tool_error = await client.call_tool("get_product", {"product_id": "MISSING"})
            bad_args = await client.call_tool("get_product", {"product_id": 7})
            return client.protocol_version, client.server_info, listed, ok, wrapped, tool_error, bad_args

    version, server_info, listed, ok, wrapped, tool_error, bad_args = asyncio.run(go())

    # mode="auto" probes server/discover first; getting 2026-07-28 back proves discover succeeded
    # (a failed probe would have fallen back to the legacy initialize handshake).
    assert version == "2026-07-28"
    assert server_info is not None and server_info.name == "catalog"

    assert [tool.name for tool in listed.tools] == ["get_product", "search_products"]
    assert listed.tools[1].input_schema["required"] == ["query"]

    assert ok.is_error is False
    assert ok.structured_content == {"product_id": "TNT-ALPINE-2", "name": "Alpine 2 tent"}
    assert json.loads(ok.content[0].text) == ok.structured_content

    assert wrapped.structured_content == {"result": ["tent", "tent"]}
    assert tool_error.is_error is True and tool_error.content[0].text == "no such product"
    assert bad_args.is_error is True


def test_sdk_client_surfaces_our_json_rpc_errors(server_url):
    """Version pinned (no discover probe), so the SDK sends tools/list straight away and shows our 401 body."""

    async def go():
        client, http = _client(server_url, token="wrong", mode="2026-07-28")
        async with http, client:
            await client.list_tools()

    assert _raised_mcp_error(go).error.code == mcp.UNAUTHORIZED


def test_sdk_auto_mode_falls_back_to_initialize_which_we_reject(server_url):
    """With a bad token, discover gets 401 and the SDK falls back to the legacy `initialize` handshake.

    That request has no MCP-Protocol-Version header, so our check #1 answers -32020 before any auth runs.
    """

    async def go():
        client, http = _client(server_url, token="wrong")
        async with http, client:
            pass

    assert _raised_mcp_error(go).error.code == mcp.HEADER_MISMATCH
