"""Request ID and caller identity, kept in context variables for logs and outbound calls."""

import time
import traceback
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders

from copperline_common.jwt import bearer_token, unverified_claims

REQUEST_ID_HEADER = "x-request-id"
ORIGIN_HEADER = "x-copperline-origin"

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
agent_id_var: ContextVar[str | None] = ContextVar("agent_id", default=None)
obo_sub_var: ContextVar[str | None] = ContextVar("obo_sub", default=None)

_INTERNAL_ERROR_BODY = b'{"error":"internal_error"}'


def new_request_id(prefix: str | None = None) -> str:
    request_id = uuid.uuid4().hex
    return f"{prefix}-{request_id}" if prefix else request_id


def current_request_id() -> str:
    """The request ID of the current context. Generates (and keeps) one if there is none."""
    request_id = request_id_var.get()
    if not request_id:
        request_id = new_request_id()
        request_id_var.set(request_id)
    return request_id


def identity_from_authorization(authorization: str | None) -> tuple[str | None, str | None]:
    """(agent_id, obo_sub) from an UNVERIFIED bearer token. For logs only, never for decisions."""
    token = bearer_token(authorization)
    claims = unverified_claims(token) if token else {}
    if claims.get("agent_id"):
        return _text(claims.get("agent_id")), _text(claims.get("obo_sub"))
    # No agent_id: a user (or service/partner) token, so the human is the token subject.
    return None, _text(claims.get("sub"))


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _log(event: str, **fields: object) -> None:
    # Imported here because logging.py imports the context variables from this module.
    from copperline_common.logging import log_event

    log_event(event, **fields)


class RequestIdMiddleware:
    """Pure ASGI middleware: request ID, identity, origin header, 500 handling and one log line per request."""

    def __init__(self, app, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(REQUEST_ID_HEADER) or new_request_id()
        agent_id, obo_sub = identity_from_authorization(headers.get("authorization"))
        resets = [
            (request_id_var, request_id_var.set(request_id)),
            (agent_id_var, agent_id_var.set(agent_id)),
            (obo_sub_var, obo_sub_var.set(obo_sub)),
        ]
        response = {"status": None, "started": False}
        started_at = time.monotonic()

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                response["started"] = True
                message.setdefault("headers", [])
                out = MutableHeaders(scope=message)
                out[REQUEST_ID_HEADER] = request_id
                out[ORIGIN_HEADER] = self.service
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            _log(
                "unhandled_exception",
                level="error",
                method=scope.get("method"),
                path=scope.get("path"),
                traceback=traceback.format_exc(),
            )
            if not response["started"]:
                await _send_internal_error(send_with_headers)
            else:
                response["status"] = response["status"] or 500
        finally:
            if not (scope.get("method") == "GET" and scope.get("path") == "/healthz"):
                _log(
                    "http_request",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status=response["status"],
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                )
            for var, token in resets:
                var.reset(token)


async def _send_internal_error(send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_INTERNAL_ERROR_BODY)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _INTERNAL_ERROR_BODY})
