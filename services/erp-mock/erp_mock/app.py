"""erp-mock HTTP APIs. Contract: services/erp-mock/README.md.

Two FastAPI apps share one Db:
    create_business_app(settings)  port 8081, service token required; reached through XC erp-lb
    create_admin_app(settings)     port 8181, no auth; published on the host's loopback only (cockpit, reset)

Creating an app without a Db is a service start: it rebuilds the process's one Db for settings.db_path from seed.sql
(README: "At startup ... the database is rebuilt"), so build both apps before serving either. `python -m erp_mock`
builds the Db once and passes it to both.

Endpoints are plain `def`, so FastAPI runs them in its thread pool; Db serializes them with a lock.
"""

import time
from typing import Annotated

from copperline_common.logging import log_event, setup_logging
from copperline_common.service import setup_service
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from erp_mock.auth import ServiceTokenAuth
from erp_mock.db import Db, InsufficientStock, NotFound, shared_db
from erp_mock.settings import Settings

__all__ = ["Settings", "create_admin_app", "create_business_app"]

SERVICE = "erp-mock"
ADMIN_SERVICE = "erp-mock-admin"
HTTP_ERROR_CODES = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}


# ---- request bodies -----------------------------------------------------------------------------------------------
# Strict numbers: JSON true is not a delta of 1, and "2" is not a quantity. Anything else wrong is a 422.
# Bounds keep values inside SQLite's 64-bit integer range, so a silly number is a 422, never a 500.
MAX_UNITS = 1_000_000
MAX_AMOUNT = 1_000_000.0


class AdjustBody(BaseModel):
    sku: str
    store_id: str
    delta: Annotated[StrictInt, Field(ge=-MAX_UNITS, le=MAX_UNITS)]
    reason: str | None = None


class TransferBody(BaseModel):
    sku: str
    from_store: str
    to_store: str
    qty: Annotated[StrictInt, Field(gt=0, le=MAX_UNITS)]

    @model_validator(mode="after")
    def different_stores(self) -> "TransferBody":
        if self.from_store == self.to_store:
            raise ValueError("from_store and to_store must differ")
        return self


class RefundBody(BaseModel):
    order_id: str
    amount: Annotated[float, Field(strict=True, gt=0, le=MAX_AMOUNT, allow_inf_nan=False)]
    reason: str | None = None


# ---- apps ---------------------------------------------------------------------------------------------------------


def create_business_app(settings: Settings, db: Db | None = None) -> FastAPI:
    """The ERP API the tool servers call. Pass the same Db to create_admin_app()."""
    app = FastAPI(title="copperline-erp-mock", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db if db is not None else shared_db(settings.db_path, settings.seed_path, rebuild=True)
    # Added before setup_service, so the request-ID middleware wraps it and also logs (and tags) 401s and 403s.
    app.add_middleware(ServiceTokenAuth, verifier=settings.verifier())
    setup_service(app, SERVICE)
    add_error_handlers(app)
    app.add_api_route("/orders/{order_id}", get_order, methods=["GET"])
    app.add_api_route("/customers/{customer_id}/orders", customer_orders, methods=["GET"])
    app.add_api_route("/inventory/adjust", adjust_stock, methods=["POST"])
    app.add_api_route("/inventory/transfer", transfer_stock, methods=["POST"])
    app.add_api_route("/inventory/{sku}", get_stock, methods=["GET"])
    app.add_api_route("/refunds", issue_refund, methods=["POST"], status_code=201)
    app.add_api_route("/refunds/{refund_id}", get_refund, methods=["GET"])
    return app


def create_admin_app(settings: Settings, db: Db | None = None) -> FastAPI:
    """The presenter's view of the estate. No auth: its only protection is the loopback-only port mapping."""
    app = FastAPI(title="copperline-erp-mock-admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db if db is not None else shared_db(settings.db_path, settings.seed_path, rebuild=True)
    setup_service(app, ADMIN_SERVICE)  # /healthz and x-copperline-origin say erp-mock-admin ...
    setup_logging(SERVICE)  # ... but it is one process, so every log line keeps service=erp-mock
    add_error_handlers(app)
    app.add_api_route("/admin/state", admin_state, methods=["GET"])
    app.add_api_route("/admin/order/{order_id}", admin_order, methods=["GET"])
    app.add_api_route("/admin/reset", admin_reset, methods=["POST"])
    return app


def add_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NotFound, not_found)
    app.add_exception_handler(InsufficientStock, insufficient_stock)
    app.add_exception_handler(StarletteHTTPException, http_error)


async def not_found(request: Request, exc: NotFound) -> JSONResponse:
    return JSONResponse({"error": "not_found", "detail": str(exc)}, status_code=404)


async def insufficient_stock(request: Request, exc: InsufficientStock) -> JSONResponse:
    return JSONResponse({"error": "insufficient_stock", "detail": str(exc)}, status_code=409)


async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Unknown routes (404) and wrong methods (405) get the same {"error", "detail"} shape."""
    body = {"error": HTTP_ERROR_CODES.get(exc.status_code, "http_error"), "detail": str(exc.detail)}
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


def db_of(request: Request) -> Db:
    return request.app.state.db


def actor_of(request: Request) -> str | None:
    """The verified token's sub (set by ServiceTokenAuth): who made the write."""
    return request.state.claims.get("sub")


# ---- business routes ----------------------------------------------------------------------------------------------


def get_order(order_id: str, request: Request) -> dict:
    return db_of(request).get_order(order_id)  # full PAN: mcp-orders decides what to mask; Data Guard masks (S2)


def customer_orders(customer_id: str, request: Request) -> dict:
    return {"customer_id": customer_id, "orders": db_of(request).customer_orders(customer_id)}


def get_stock(sku: str, request: Request, store_id: str | None = None) -> dict:
    if store_id:
        return db_of(request).stock_at(sku, store_id)
    return db_of(request).stock_everywhere(sku)


def adjust_stock(body: AdjustBody, request: Request) -> dict:
    actor = actor_of(request)
    result = db_of(request).adjust_stock(body.sku, body.store_id, body.delta, body.reason, actor)
    log_event("erp_write", status=200, kind="stock_adjust", actor=actor, sku=body.sku, store_id=body.store_id,
              delta=body.delta, qty=result["qty"])
    return result


def transfer_stock(body: TransferBody, request: Request) -> dict:
    actor = actor_of(request)
    result = db_of(request).transfer_stock(body.sku, body.from_store, body.to_store, body.qty, actor)
    log_event("erp_write", status=200, kind="stock_transfer", actor=actor, sku=body.sku,
              from_store=body.from_store, to_store=body.to_store, qty=body.qty)
    return result


def issue_refund(body: RefundBody, request: Request) -> dict:
    actor = actor_of(request)
    result = db_of(request).issue_refund(body.order_id, body.amount, body.reason, actor)
    # order_id and amount only: never the card number or the customer's email.
    log_event("erp_write", status=201, kind="refund", actor=actor, order_id=body.order_id, amount=body.amount,
              refund_id=result["refund_id"])
    return result


def get_refund(refund_id: str, request: Request) -> dict:
    return db_of(request).get_refund(refund_id)


# ---- admin routes -------------------------------------------------------------------------------------------------


def admin_state(request: Request) -> dict:
    return db_of(request).state()


def admin_order(order_id: str, request: Request) -> dict:
    return db_of(request).raw_order(order_id)


def admin_reset(request: Request) -> dict:
    started = time.monotonic()
    db_of(request).reset()
    latency_ms = int((time.monotonic() - started) * 1000)
    log_event("erp_reset", status=200, latency_ms=latency_ms)
    return {"status": "reset", "refunds_issued": 0, "stock_writes": 0}
