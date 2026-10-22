"""python -m cockpit: serve the cockpit. Locally on 127.0.0.1:8095 (no gateway in front: cockpit-lb is not emulated)."""

import uvicorn
from copperline_common.logging import log_event, setup_logging

from cockpit.app import SERVICE, create_app
from cockpit.settings import Settings


def main() -> None:
    setup_logging(SERVICE)
    try:
        settings = Settings.from_env()
        app = create_app(settings)
    except (RuntimeError, OSError, ValueError) as e:
        log_event("cockpit_config_error", level="error", reason=f"{type(e).__name__}: {e}")
        raise SystemExit(2) from None
    print(f"cockpit: http://{settings.host}:{settings.port}  (env {settings.env})", flush=True)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
