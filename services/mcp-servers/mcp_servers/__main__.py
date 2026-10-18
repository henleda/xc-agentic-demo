"""python -m mcp_servers: run the server named by MCP_SERVER on 0.0.0.0:${PORT:-8080}."""

import os

import uvicorn

from mcp_servers.app import Settings, create_app


def main() -> None:
    app = create_app(Settings.from_env())
    # No access log: the request-ID middleware already logs one http_request line per call.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_config=None,
                access_log=False, server_header=False)


if __name__ == "__main__":
    main()
