"""`python -m ai_gw`: one process, two uvicorn servers sharing one Usage.

    gateway API  0.0.0.0:${AIGW_PORT:-8090}        (XC llm-lb)
    admin API    0.0.0.0:${AIGW_ADMIN_PORT:-8190}  (compose publishes it on 127.0.0.1 only)
"""

import asyncio

import uvicorn
from copperline_common.logging import log_event, setup_logging

from ai_gw.app import SERVICE, create_admin_app, create_gateway_app
from ai_gw.settings import Settings
from ai_gw.usage import Usage


def server(app, host: str, port: int) -> uvicorn.Server:
    # log_config=None: uvicorn's lines go through our JSON root logger. No access log: the request-ID middleware
    # already logs one http_request line per call.
    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False, server_header=False)
    return uvicorn.Server(config)


async def serve(settings: Settings) -> None:
    usage = Usage(settings.budgets)
    gateway = server(create_gateway_app(settings, usage), settings.host, settings.port)
    admin = server(create_admin_app(settings, usage), settings.host, settings.admin_port)
    log_event("aigw_ready", mode="stub" if settings.stub_model else "bedrock", port=settings.port,
              admin_port=settings.admin_port, routes=sorted(settings.route_models), budgets=settings.budgets,
              require_key=settings.require_key, unknown_routes=settings.unknown_routes)
    # On SIGTERM uvicorn stops the server that caught it and re-raises the signal, which stops the other one.
    await asyncio.gather(gateway.serve(), admin.serve())


def main() -> None:
    setup_logging(SERVICE)
    try:
        settings = Settings.from_env()
    except RuntimeError as e:
        log_event("aigw_config_error", level="error", reason=str(e))
        raise SystemExit(2) from None
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
