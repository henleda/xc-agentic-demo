"""JSON logs: one object per line on stdout, with the standard fields always present."""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from copperline_common.requestid import agent_id_var, obo_sub_var, request_id_var

SENSITIVE_MARKERS = ("secret", "token", "password", "authorization", "api_key")
REDACTED = "***"

_service: str | None = None
_handler: logging.Handler | None = None
_logger = logging.getLogger("copperline")


def is_sensitive(key: str) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def redact(value: Any) -> Any:
    """Replace the value of every sensitive key with "***", recursively."""
    if isinstance(value, dict):
        return {key: REDACTED if is_sensitive(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _level_number(name: str) -> int:
    return logging.getLevelNamesMapping().get(name.upper(), logging.INFO)


def _base_fields(event: str, level: str) -> dict[str, Any]:
    return {
        "ts": _now_iso(),
        "service": _service,
        "level": level,
        "request_id": request_id_var.get(),
        "agent_id": agent_id_var.get(),
        "obo_sub": obo_sub_var.get(),
        "event": event,
        "status": None,
        "latency_ms": None,
    }


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "copperline", None)
        if fields is None:
            # A plain stdlib logging call (uvicorn, httpx, ...): wrap it in the same shape.
            fields = _base_fields("log", record.levelname.lower())
            fields["logger"] = record.name
            fields["message"] = record.getMessage()
        if record.exc_info:
            fields = {**fields, "traceback": self.formatException(record.exc_info)}
        return json.dumps(fields, default=str)


class _StdoutHandler(logging.Handler):
    """Looks up sys.stdout on every write, so redirected or captured stdout keeps working."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


def setup_logging(service: str, level: str | None = None) -> None:
    """Send the root logger to stdout as JSON. Safe to call more than once."""
    global _service, _handler
    _service = service
    if _handler is None:
        _handler = _StdoutHandler()
        _handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)
    root.setLevel(_level_number(level or os.environ.get("LOG_LEVEL") or "info"))


def log_event(event: str, *, level: str = "info", **fields: Any) -> None:
    """Log one event. Extra keyword arguments become top-level keys (sensitive ones are redacted)."""
    record = _base_fields(event, level)
    record.update(redact(fields))
    _logger.log(_level_number(level), event, extra={"copperline": record})
