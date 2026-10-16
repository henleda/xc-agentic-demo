"""Outbound HTTP that propagates x-request-id and never raises."""

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from copperline_common.requestid import REQUEST_ID_HEADER, current_request_id


@dataclass
class HttpResult:
    status: int | None  # None = no HTTP response at all
    headers: dict[str, str]  # lowercased keys
    body_text: str
    json: Any | None  # parsed body if it is valid JSON, else None
    latency_ms: int
    error: str | None = None  # "connect_error: ...", "dns: ...", "timeout", ... when status is None


async def _add_request_id(request: httpx.Request) -> None:
    if REQUEST_ID_HEADER not in request.headers:
        request.headers[REQUEST_ID_HEADER] = current_request_id()


def new_http_client(timeout: float = 10.0, **kwargs) -> httpx.AsyncClient:
    """An AsyncClient that adds x-request-id (from the current context) to every request."""
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["request"] = [_add_request_id, *hooks.get("request", [])]
    return httpx.AsyncClient(timeout=timeout, event_hooks=hooks, **kwargs)


def _parse_json(text: str) -> Any | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _describe_transport_error(exc: httpx.TransportError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    message = str(exc) or type(exc).__name__
    lowered = message.lower()
    if isinstance(exc, httpx.ConnectError):
        dns_markers = ("name or service not known", "nodename nor servname", "name resolution", "getaddrinfo")
        if any(marker in lowered for marker in dns_markers):
            return f"dns: {message}"
        return f"connect_error: {message}"
    return f"{type(exc).__name__}: {message}"


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


async def send(
    client: httpx.AsyncClient, method: str, url: str, *, retry_connect_once: bool = False, **kwargs
) -> HttpResult:
    """Send one request. HTTP errors and transport errors come back as an HttpResult, never as exceptions.

    retry_connect_once retries exactly once on httpx.ConnectError (never on a timeout).
    """
    started_at = time.monotonic()
    try:
        response = await _request(client, method, url, retry_connect_once, **kwargs)
    except httpx.TransportError as exc:
        error = _describe_transport_error(exc)
    except httpx.InvalidURL as exc:
        error = f"invalid_url: {exc}"
    else:
        text = response.text
        return HttpResult(
            status=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body_text=text,
            json=_parse_json(text),
            latency_ms=_elapsed_ms(started_at),
        )
    return HttpResult(status=None, headers={}, body_text="", json=None, latency_ms=_elapsed_ms(started_at), error=error)


async def _request(client: httpx.AsyncClient, method: str, url: str, retry_connect_once: bool, **kwargs):
    try:
        return await client.request(method, url, **kwargs)
    except httpx.ConnectError:
        if not retry_connect_once:
            raise
    return await client.request(method, url, **kwargs)
