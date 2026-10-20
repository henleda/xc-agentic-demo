# agent-runtime image: one image for every agent; AGENT_CONFIG picks which, MODE=batch runs the job (S5).
# Build from the repo root:
#   docker build -f services/agent-runtime/Dockerfile .
# The scenarios are baked in at /app/scenarios (services/agent-runtime/README.md).
# Runs as any UID with group 0 and a read-only root filesystem (CLAUDE.md rule 3); only /tmp needs to be writable.
FROM ghcr.io/astral-sh/uv:0.6.14 AS uv

FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# The whole uv workspace is needed so --locked can check uv.lock; only this package and its deps are installed.
COPY pyproject.toml uv.lock ./
COPY services/ services/
COPY deploy/local/local-gateway/ deploy/local/local-gateway/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package copperline-agent-runtime

FROM python:3.12-slim-bookworm
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CONFIG_DIR=/config \
    SCENARIOS_DIR=/app/scenarios
WORKDIR /app
COPY scenarios/ /app/scenarios/
RUN chgrp -R 0 /app && chmod -R g=u /app
USER 10001
EXPOSE 8080
CMD ["python", "-m", "agent_runtime"]
