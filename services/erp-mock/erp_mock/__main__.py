"""`python -m erp_mock`: one process, two uvicorn servers sharing one database.

    business API  0.0.0.0:${ERP_PORT:-8081}        (XC erp-lb)
    admin API     0.0.0.0:${ERP_ADMIN_PORT:-8181}  (compose publishes it on 127.0.0.1 only)
"""

import asyncio

import uvicorn
from copperline_common.logging import log_event, setup_logging

from erp_mock.app import SERVICE, create_admin_app, create_business_app
from erp_mock.db import shared_db
from erp_mock.settings import Settings


def build_servers(settings: Settings) -> tuple[uvicorn.Server, uvicorn.Server]:
    """(business, admin), both on one Db, which is built from seed.sql right here."""
    db = shared_db(settings.db_path, settings.seed_path)
    business = server(create_business_app(settings, db), settings.host, settings.port)
    admin = server(create_admin_app(settings, db), settings.host, settings.admin_port)
    log_event("erp_ready", port=settings.port, admin_port=settings.admin_port, db_path=str(settings.db_path),
              jwks=settings.jwks_source(), state=db.state())
    return business, admin


def server(app, host: str, port: int) -> uvicorn.Server:
    # log_config=None: uvicorn's own lines go through our JSON root logger. No access log: the request-ID
    # middleware already logs one http_request line per call.
    config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False, server_header=False)
    return uvicorn.Server(config)


async def serve(business: uvicorn.Server, admin: uvicorn.Server) -> None:
    # On SIGTERM uvicorn stops the server that caught it and re-raises the signal, which stops the other one.
    await asyncio.gather(business.serve(), admin.serve())


def main() -> None:
    setup_logging(SERVICE)
    try:
        settings = Settings.from_env()
    except RuntimeError as e:
        log_event("erp_config_error", level="error", reason=str(e))
        raise SystemExit(2) from None
    asyncio.run(serve(*build_servers(settings)))


if __name__ == "__main__":
    main()
