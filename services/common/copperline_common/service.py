"""One call that gives every service the shared conventions."""

from fastapi import FastAPI

from copperline_common.logging import setup_logging
from copperline_common.requestid import RequestIdMiddleware


def setup_service(app: FastAPI, service: str) -> None:
    """JSON logging, request-ID/origin middleware and GET /healthz."""
    setup_logging(service)
    app.add_middleware(RequestIdMiddleware, service=service)

    async def healthz() -> dict:
        return {"status": "ok", "service": service}

    app.add_api_route("/healthz", healthz, methods=["GET"], include_in_schema=False)
