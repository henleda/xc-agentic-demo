"""One call that gives every service the shared conventions."""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from copperline_common.logging import setup_logging
from copperline_common.requestid import RequestIdMiddleware


def setup_service(app: FastAPI, service: str) -> None:
    """JSON logging, request-ID/origin middleware and GET /healthz."""
    setup_logging(service)
    app.add_middleware(RequestIdMiddleware, service=service)

    async def healthz() -> dict:
        return {"status": "ok", "service": service}

    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
    app.add_exception_handler(RequestValidationError, _validation_error)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 echoes the rejected input. NaN/Infinity can't be JSON-encoded, so that default turns a
    bad request into a 500. Same body shape, with the inputs removed (they may also hold PII)."""
    errors = [{key: value for key, value in error.items() if key not in ("input", "ctx")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
