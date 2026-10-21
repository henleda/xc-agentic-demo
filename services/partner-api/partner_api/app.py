"""partner-api HTTP API. Contract: services/partner-api/README.md and deploy/xc/openapi/partner-api.yaml.

Documented (in the published OpenAPI):
    GET  /v1/purchase-orders?page=&status=    25 POs per page
    GET  /v1/purchase-orders/{po_id}
    POST /v1/asn                              202, stored in memory
Shadow (implemented, deliberately NOT in the OpenAPI; S6 shows XC blocking and discovering them):
    GET  /v1/internal/cost-sheet
    GET  /v1/internal/margins

Every /v1 route needs a partner token. XC validates it at partner-lb; this service checks it again (signature, exp,
iss, aud copperline-partner, role partner), so a request that reaches the pod some other way gets nothing.
"""

import itertools
import re
import threading
from typing import Annotated, Literal

from copperline_common.jwt import JwksVerifier, JwtError, bearer_token
from copperline_common.logging import log_event
from copperline_common.service import setup_service
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

from partner_api import data
from partner_api.settings import Settings

SERVICE = "partner-api"
AUDIENCE = "copperline-partner"
REQUIRED_ROLE = "partner"
PO_ID = re.compile(r"^PO-[0-9]{4}$")
MAX_ASNS = 1000  # in memory only; the oldest are dropped
MAX_BODY_BYTES = 64 * 1024  # an ASN is a few hundred bytes; this keeps one request far below the pod's memory limit
MAX_PO_ID = 200  # also in the published spec (maxLength), so XC and the service agree
MAX_ITEMS = 200  # also in the published spec (maxItems)
KEYS_UNAVAILABLE = "token keys unavailable"  # what the caller sees; the real error stays in the log
HTTP_ERROR_CODES = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}


class AsnBody(BaseModel):
    # No pattern on po_id, on purpose and like the spec: S6(c)'s SQLi string must reach the WAF, not schema validation.
    po_id: str = Field(max_length=MAX_PO_ID)
    carrier: str = Field(max_length=32)
    items: list[dict] = Field(max_length=MAX_ITEMS)


class PartnerTokenAuth:
    """ASGI middleware: every path except /healthz needs a valid partner token. Runs before routing, so an
    unauthenticated caller learns nothing about which paths exist (no 404 vs 422 difference). It also caps the request
    body at MAX_BODY_BYTES (413), counting the bytes actually received, so a chunked body is capped too."""

    def __init__(self, app, verifier: JwksVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authorization = headers.getlist("authorization")
        if len(authorization) > 1:
            # The request-ID middleware logs identity from the first copy; never verify a different one.
            await _deny(scope, receive, send, 401, "repeated Authorization header")
            return
        token = bearer_token(authorization[0] if authorization else None)
        if token is None:
            await _deny(scope, receive, send, 401, "missing bearer token")
            return
        try:
            claims = await run_in_threadpool(self.verifier.verify, token, AUDIENCE)
        except JwtError as e:
            if str(e).startswith(("jwks unavailable", "no jwks configured", "bad jwk")):
                # Our fault, not the caller's. The caller is an outside party: no internal URL or error text.
                await _deny(scope, receive, send, 503, KEYS_UNAVAILABLE, log_reason=str(e))
            else:
                await _deny(scope, receive, send, 401, str(e))
            return
        if claims.get("role") != REQUIRED_ROLE:
            await _deny(scope, receive, send, 403, f"token lacks role={REQUIRED_ROLE}")
            return
        scope.setdefault("state", {})["claims"] = claims
        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            await _deny(scope, receive, send, 413, f"body larger than {MAX_BODY_BYTES} bytes")
            return
        await self._capped(scope, receive, send)

    async def _capped(self, scope, receive, send) -> None:
        """Read the body here, at most MAX_BODY_BYTES actually received (so a chunked body or a lying Content-Length
        is capped too), then hand it to the app in one piece. Beyond the cap: 413, and the rest is never read."""
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > MAX_BODY_BYTES:
                await _deny(scope, receive, send, 413, f"body larger than {MAX_BODY_BYTES} bytes")
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()  # after the body: wait for a disconnect like any ASGI app would

        await self.app(scope, replay, send)


async def _deny(scope, receive, send, status: int, detail: str, *, log_reason: str | None = None) -> None:
    log_event("partner_auth_deny", level="error" if status >= 500 else "warning", status=status, path=scope["path"],
              reason=log_reason or detail)
    error = {401: "unauthorized", 403: "forbidden", 413: "payload_too_large"}.get(status, "unavailable")
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    await JSONResponse({"error": error, "detail": detail}, status_code=status, headers=headers)(scope, receive, send)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="copperline-partner-api", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.asns = []
    app.state.asn_ids = itertools.count(1)  # never reused, even after the oldest ASNs are dropped
    app.state.asn_lock = threading.Lock()
    # Added before setup_service, so the request-ID middleware wraps it and also logs and tags 401s and 403s.
    app.add_middleware(PartnerTokenAuth, verifier=settings.verifier())
    setup_service(app, SERVICE)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_api_route("/v1/purchase-orders", list_purchase_orders, methods=["GET"])
    app.add_api_route("/v1/purchase-orders/{po_id}", get_purchase_order, methods=["GET"])
    app.add_api_route("/v1/asn", submit_asn, methods=["POST"], status_code=202)
    app.add_api_route("/v1/internal/cost-sheet", cost_sheet, methods=["GET"])
    app.add_api_route("/v1/internal/margins", margins, methods=["GET"])
    log_event("partner_api_ready", jwks=settings.jwks_source(), purchase_orders=len(data.PURCHASE_ORDERS))
    return app


async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse({"error": HTTP_ERROR_CODES.get(exc.status_code, "error"), "detail": str(exc.detail)},
                        status_code=exc.status_code)


# ---- documented operations ----------------------------------------------------------------------------------------


def list_purchase_orders(page: Annotated[int, Query(ge=1, le=100)] = 1,
                         status: Literal["open", "shipped", "closed"] | None = None) -> dict:
    return data.page_of(page, status)


def get_purchase_order(po_id: str) -> dict:
    if not PO_ID.match(po_id) or po_id not in data.BY_ID:
        raise StarletteHTTPException(404, "no such purchase order")
    return data.BY_ID[po_id]


def submit_asn(body: AsnBody, request: Request) -> dict:
    """Stores the notice in memory and echoes po_id back. No SQL anywhere: the S6(c) point is the WAF seeing the
    payload, not a real vulnerability behind it."""
    record = {"asn_id": None, "po_id": body.po_id, "carrier": body.carrier, "items": len(body.items),
              "submitted_by": request.state.claims.get("sub")}
    with request.app.state.asn_lock:
        asns = request.app.state.asns
        record["asn_id"] = f"ASN-{next(request.app.state.asn_ids):05d}"
        asns.append(record)
        del asns[:-MAX_ASNS]
    log_event("asn_received", asn_id=record["asn_id"], items=record["items"])
    return {"asn_id": record["asn_id"], "po_id": body.po_id, "status": "accepted"}


# ---- shadow operations (not in the published OpenAPI) -----------------------------------------------------------


def cost_sheet() -> dict:
    log_event("shadow_endpoint_hit", path="/v1/internal/cost-sheet")
    return data.COST_SHEET


def margins() -> dict:
    log_event("shadow_endpoint_hit", path="/v1/internal/margins")
    return data.MARGINS
