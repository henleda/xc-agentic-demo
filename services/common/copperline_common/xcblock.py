"""Who blocked a call: the XC fabric, our own server, or the network?

Seeded heuristics (services/common/README.md, "xcblock.classify rules"). Replace the WAF signature
with the real block samples recorded in docs/08 during Phase 3.
"""

from copperline_common.outbound import HttpResult
from copperline_common.requestid import ORIGIN_HEADER

LOCAL_GATEWAY_HEADER = "x-local-gateway"

# TODO(verify-xc): seeded guesses at the XC WAF block page. Replace with the samples in docs/08 (R32).
WAF_SIGNATURES = ("the requested url was rejected", "support id", "request rejected")

REASON_NETWORK_NO_RESPONSE = "no response"
REASON_UPSTREAM_UNREACHABLE = "upstream unreachable behind LB"


def classify(result: HttpResult, *, via_lb: bool) -> tuple[str | None, str | None]:
    """(blocked_by, reason). blocked_by is "xc", "server", "network" or None."""
    status = result.status
    if status is None:
        return "network", result.error or REASON_NETWORK_NO_RESPONSE
    if status < 400:
        return None, None
    if ORIGIN_HEADER in result.headers:
        return "server", _server_reason(result)
    if not via_lb:
        return None, None
    if status >= 500:
        return "network", f"{REASON_UPSTREAM_UNREACHABLE} (HTTP {status})"
    return "xc", _fabric_reason(result)


def _server_reason(result: HttpResult) -> str:
    """"<service>: <message>" from a JSON-RPC error, an OAuth-style error or a FastAPI detail."""
    origin = result.headers[ORIGIN_HEADER]
    body = result.json if isinstance(result.json, dict) else {}
    error = body.get("error")
    if isinstance(error, dict) and error.get("message"):
        return f"{origin}: {error['message']}"
    if isinstance(error, str):
        return f"{origin}: {error}"
    if isinstance(body.get("detail"), str):
        return f"{origin}: {body['detail']}"
    return f"{origin}: HTTP {result.status}"


def _is_local_gateway(result: HttpResult) -> bool:
    return LOCAL_GATEWAY_HEADER in result.headers or LOCAL_GATEWAY_HEADER in result.body_text


def _fabric_reason(result: HttpResult) -> str:
    status = result.status
    body = result.body_text.lower()
    if status == 429:
        reason = "rate limited"
    elif status == 401:
        reason = "jwt validation"
    elif any(signature in body for signature in WAF_SIGNATURES):
        reason = "waf"
    elif status == 403:
        reason = "service policy"
    else:
        reason = f"HTTP {status}"
    if _is_local_gateway(result):
        return f"local-gateway (NOT XC): {reason}"
    return reason


def _short_server_reason(reason: str | None) -> str | None:
    # Server reasons look like "<service>: <message>"; the model only needs the message.
    if not reason:
        return None
    return reason.split(": ", 1)[-1]


def model_feedback(kind: str, status: int | None, blocked_by: str | None, reason: str | None) -> str:
    """A one-line note for the model about a call's outcome. kind is the step kind ("tool", "model", ...)."""
    action = {"tool": "Tool call", "model": "Model call"}.get(kind, "Request")
    if status is None:
        return "Network error: no response."
    if blocked_by == "xc":
        if status == 429:
            return f"Rate limited by the platform (HTTP {status})."
        return f"{action} denied by policy (HTTP {status})."
    if blocked_by == "server":
        who = "the tool server" if kind == "tool" else "the server"
        detail = _short_server_reason(reason)
        suffix = f": {detail}" if detail else ""
        return f"{action} rejected by {who} (HTTP {status}{suffix})."
    if blocked_by == "network":
        return f"Network error: upstream unreachable (HTTP {status})."
    if status >= 400:
        return f"{action} failed (HTTP {status})."
    return f"{action} succeeded (HTTP {status})."
