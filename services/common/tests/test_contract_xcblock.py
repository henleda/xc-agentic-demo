"""Black-box contract tests for xcblock.classify and model_feedback (README "xcblock.classify rules")."""
import pytest
from copperline_common.outbound import HttpResult
from copperline_common.xcblock import LOCAL_GATEWAY_HEADER, ORIGIN_HEADER, classify, model_feedback

ORIGIN = {ORIGIN_HEADER: "mcp-orders", "content-type": "application/json"}
NO_ORIGIN = {"content-type": "text/html"}
BLOCK_PAGE = "Forbidden"
# Default F5 XC WAF block page (the README names "the XC WAF block page signature" without quoting it).
WAF_PAGE = ("<html><head><title>Request Rejected</title></head><body>The requested URL was rejected. "
            "Please consult with your administrator.<br/><br/>Your support ID is: 1234567890<br/><br/>"
            "<a href='javascript:history.back();'>[Go Back]</a></body></html>")


def result(status, headers=None, body="", error=None):
    return HttpResult(status=status, headers=dict(headers or {}), body_text=body, json=None, latency_ms=5, error=error)


def blocked_by(res, via_lb):
    return classify(res, via_lb=via_lb)[0]


def test_constants():
    assert ORIGIN_HEADER == "x-copperline-origin"
    assert LOCAL_GATEWAY_HEADER == "x-local-gateway"


# --- row 1: no HTTP response -----------------------------------------------------------------------


@pytest.mark.parametrize("error", ["connect_error: connection refused", "dns: name not known", "timeout"])
@pytest.mark.parametrize("via_lb", [True, False])
def test_no_http_response_is_network(error, via_lb):
    assert blocked_by(result(None, error=error), via_lb) == "network"


# --- row 2: 2xx / 3xx ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 201, 204, 301, 302, 304])
@pytest.mark.parametrize("headers", [ORIGIN, NO_ORIGIN])
@pytest.mark.parametrize("via_lb", [True, False])
def test_success_and_redirect_are_not_blocked(status, headers, via_lb):
    assert blocked_by(result(status, headers), via_lb) is None


# --- row 3: our service answered -------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
def test_origin_header_via_lb_is_server(status):
    assert blocked_by(result(status, ORIGIN, '{"jsonrpc":"2.0","id":1,"error":{}}'), True) == "server"


def test_s1b_header_mismatch_400_with_origin_is_server():
    body = '{"jsonrpc":"2.0","id":"1","error":{"code":-32020,"message":"HeaderMismatch","data":{"header":"Mcp-Name"}}}'
    assert blocked_by(result(400, ORIGIN, body), True) == "server"


# --- row 4: fabric 4xx -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 413, 429])
def test_4xx_without_origin_via_lb_is_xc(status):
    assert blocked_by(result(status, NO_ORIGIN, BLOCK_PAGE), True) == "xc"


def test_xc_reasons_distinguish_jwt_policy_and_rate_limit():
    _, reason_401 = classify(result(401, NO_ORIGIN, "Jwt is missing"), via_lb=True)
    _, reason_403 = classify(result(403, NO_ORIGIN, BLOCK_PAGE), via_lb=True)
    _, reason_429 = classify(result(429, NO_ORIGIN, "Too Many Requests"), via_lb=True)
    assert "jwt validation" in reason_401.lower()
    assert "service policy" in reason_403.lower()
    assert "rate limited" in reason_429.lower()


def test_xc_waf_block_page_reason_is_waf():
    kind, reason = classify(result(403, NO_ORIGIN, WAF_PAGE), via_lb=True)
    assert kind == "xc"
    assert "waf" in reason.lower()


def test_local_gateway_block_is_xc_but_labelled_not_xc():
    headers = {**NO_ORIGIN, LOCAL_GATEWAY_HEADER: "1", "content-type": "application/json"}
    body = '{"error": "forbidden", "x-local-gateway": true}'
    kind, reason = classify(result(403, headers, body), via_lb=True)
    assert kind == "xc"
    assert reason.startswith("local-gateway (NOT XC)")


# --- row 5: 5xx behind the LB ----------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_without_origin_via_lb_is_network(status):
    assert blocked_by(result(status, NO_ORIGIN, "upstream connect error"), True) == "network"


# --- row 6: not one of our LBs ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502])
def test_not_via_lb_without_origin_is_never_blocked(status):
    assert blocked_by(result(status, NO_ORIGIN, BLOCK_PAGE), False) is None


@pytest.mark.parametrize("status", [200, 400, 401, 403, 429, 500])
@pytest.mark.parametrize("headers", [ORIGIN, NO_ORIGIN])
def test_not_via_lb_never_yields_xc(status, headers):
    assert blocked_by(result(status, headers, BLOCK_PAGE), False) != "xc"


# --- model_feedback --------------------------------------------------------------------------------


def test_feedback_for_policy_deny():
    assert "denied by policy (http 403)" in model_feedback("tool", 403, "xc", "service policy").lower()


def test_feedback_for_rate_limit():
    assert "rate limited by the platform (http 429)" in model_feedback("tool", 429, "xc", "rate limited").lower()


def test_feedback_for_tool_server_rejection():
    text = model_feedback("tool", 400, "server", "HeaderMismatch").lower()
    assert "rejected by the tool server (http 400: headermismatch)" in text


def test_feedback_for_no_response():
    assert "network error: no response" in model_feedback("tool", None, "network", "connect_error: refused").lower()


@pytest.mark.parametrize("args", [("model", 403, "xc", "service policy"), ("http", 502, "network", None),
                                  ("tool", 404, "xc", None)])
def test_feedback_is_always_a_short_string(args):
    text = model_feedback(*args)
    assert isinstance(text, str) and 0 < len(text) <= 300
