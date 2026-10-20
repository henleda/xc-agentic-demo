"""python -m partner_api: serve on 0.0.0.0:${PORT:-8080}."""

import uvicorn
from copperline_common.logging import log_event, setup_logging

from partner_api.app import SERVICE, create_app
from partner_api.settings import Settings


def main() -> None:
    setup_logging(SERVICE)
    try:
        settings = Settings.from_env()
        app = create_app(settings)
    except (RuntimeError, ValueError) as e:
        log_event("partner_api_config_error", level="error", reason=str(e))
        raise SystemExit(2) from None
    # log_config=None: uvicorn's lines go through our JSON root logger. No access log: the request-ID middleware
    # already logs one http_request line per call.
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
