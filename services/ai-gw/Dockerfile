# ai-gw image (runs on EC2 copperline-core, not in vK8s). Build from the repo root:
#   docker build -f services/ai-gw/Dockerfile .
# Runs as any UID with group 0 and a read-only root filesystem (CLAUDE.md rule 3); only /tmp needs to be writable.
# AWS credentials are never baked in: on EC2 they come from the instance role.
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
    uv sync --locked --no-dev --no-editable --package copperline-ai-gw

FROM python:3.12-slim-bookworm
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN chgrp -R 0 /app && chmod -R g=u /app
USER 10001
EXPOSE 8090 8190
CMD ["python", "-m", "ai_gw"]
