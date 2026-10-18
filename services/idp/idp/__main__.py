"""python -m idp: serve the IdP on 0.0.0.0:${PORT:-8080}. Settings are read from env at startup (README)."""

import os

import uvicorn


def main() -> None:
    # log_config=None: uvicorn's own lines go through our JSON root logger. No access log: the request-ID
    # middleware already logs one http_request line per call.
    uvicorn.run("idp.app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_config=None,
                access_log=False, server_header=False)


if __name__ == "__main__":
    main()
