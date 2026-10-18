"""python -m local_gateway: play the XC load balancer named by LB_NAME on 0.0.0.0:${PORT:-8080}. NOT XC."""

import os

import uvicorn
from copperline_common.logging import log_event, setup_logging

from local_gateway.app import SERVICE, Settings, create_app
from local_gateway.intent import IntentError


def main() -> None:
    setup_logging(SERVICE)
    try:
        app = create_app(Settings.from_env())
    except (IntentError, RuntimeError, OSError) as e:
        log_event("gateway_config_error", level="error", reason=str(e))
        raise SystemExit(2) from None
    # log_config=None: uvicorn's own lines go through our JSON root logger. No access log: we log lb_request.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_config=None,
                access_log=False, server_header=False)


if __name__ == "__main__":
    main()
