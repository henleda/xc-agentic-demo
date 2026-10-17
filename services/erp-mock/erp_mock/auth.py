"""Business-API auth: every route except GET /healthz needs a service token (README "Auth").

XC erp-lb enforces the same rule in the tenant; this is defense in depth, and what keeps the local stack honest.
It is ASGI middleware rather than a FastAPI dependency so it runs before routing and body parsing: a caller without
a valid token always gets 401/403 (503 when we have no keys), never a 404 or 422 that tells it about the API.
"""

from copperline_common.jwt import JwksVerifier, JwtError, bearer_token
from copperline_common.logging import log_event
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

AUDIENCE = "copperline-erp"
REQUIRED_SVC_ROLE = "tool-server"
EXEMPT_PATHS = ("/healthz",)
# JwtError reasons that mean "we have no usable keys" (JWKS file missing, IdP down): our fault, not the caller's.
NO_KEYS_REASONS = ("jwks unavailable", "no jwks configured", "bad jwk")


class ServiceTokenAuth:
    """On success the verified claims are in request.state.claims (the endpoints use claims["sub"] as the actor)."""

    def __init__(self, app, verifier: JwksVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        token = bearer_token(Headers(scope=scope).get("authorization"))
        if token is None:
            await deny(scope, receive, send, 401, "unauthorized", "missing bearer token")
            return
        try:
            # verify() may fetch the JWKS over HTTP (IDP_BASE_URL mode), so keep it off the event loop.
            claims = await run_in_threadpool(self.verifier.verify, token, AUDIENCE)
        except JwtError as e:
            if str(e).startswith(NO_KEYS_REASONS):
                await deny(scope, receive, send, 503, "unavailable", f"cannot check tokens: {e}")
            else:
                await deny(scope, receive, send, 401, "unauthorized", str(e))
            return
        if claims.get("svc_role") != REQUIRED_SVC_ROLE:
            await deny(scope, receive, send, 403, "forbidden", f"token lacks svc_role={REQUIRED_SVC_ROLE}")
            return

        scope.setdefault("state", {})["claims"] = claims
        await self.app(scope, receive, send)


async def deny(scope, receive, send, status: int, error: str, detail: str) -> None:
    level = "error" if status >= 500 else "warning"
    log_event("erp_auth_deny", level=level, status=status, path=scope["path"], reason=detail)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    response = JSONResponse({"error": error, "detail": detail}, status_code=status, headers=headers)
    await response(scope, receive, send)
