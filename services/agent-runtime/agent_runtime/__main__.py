"""python -m agent_runtime: MODE=server (default) serves /v1/chat on 0.0.0.0:${PORT:-8080}; MODE=batch runs the
agent's job scenario once and exits (insights-nightly, S5)."""

import asyncio
import sys

import uvicorn
import yaml
from copperline_common.logging import log_event, setup_logging

from agent_runtime.app import create_app
from agent_runtime.batch import run_batch
from agent_runtime.settings import Settings


def main() -> None:
    setup_logging("agent-runtime")
    try:
        settings = Settings.from_env()
        if settings.mode == "batch":
            setup_logging(f"{settings.agent.agent_id}-batch")
            app = None
        else:
            app = create_app(settings)  # loads tools.yaml, personas.yaml and the scenarios: may also refuse
    except (RuntimeError, OSError, ValueError, yaml.YAMLError) as e:
        log_event("agent_config_error", level="error", reason=f"{type(e).__name__}: {e}")
        raise SystemExit(2) from None
    if app is None:
        sys.exit(asyncio.run(run_batch(settings)))
    # log_config=None: uvicorn's lines go through our JSON root logger. No access log: the request-ID middleware
    # already logs one http_request line per call.
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
