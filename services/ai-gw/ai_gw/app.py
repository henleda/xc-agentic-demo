"""ai-gw HTTP APIs. Contract: docs/03-services.md ("copperline-ai-gw") and services/ai-gw/README.md.

Two FastAPI apps share one Usage:
    create_gateway_app(settings)  port 8090, POST /v1/converse; reached only through XC llm-lb
    create_admin_app(settings)    port 8190, GET /v1/usage and POST /v1/usage/reset; host loopback only

POST /v1/converse checks, in order (first failure wins):
    1. x-api-key equals GATEWAY_API_KEY (when GATEWAY_REQUIRE_KEY)        -> 401 invalid_api_key
    2. exactly one x-model-route, and it is a known route                 -> 400 bad_route
    3. the body is a JSON object with a non-empty messages list, ...      -> 400 bad_request
    4. the route's daily token budget is not used up                      -> 429 budget_exceeded
    5. the model call                                                     -> 502 / 504 on failure
"""

import hmac
import time

from copperline_common.logging import log_event
from copperline_common.requestid import REQUEST_ID_HEADER
from copperline_common.service import setup_service
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_gw.models import BedrockModel, ModelError, StubModel
from ai_gw.settings import ROUTES, Settings
from ai_gw.usage import Usage

__all__ = ["Settings", "Usage", "create_admin_app", "create_gateway_app"]

SERVICE = "ai-gw"
ADMIN_SERVICE = "ai-gw-admin"
ROUTE_HEADER = "x-model-route"
API_KEY_HEADER = "x-api-key"
HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def create_gateway_app(settings: Settings, usage: Usage, model=None) -> FastAPI:
    """model: tests pass a fake; otherwise stub or Bedrock from settings."""
    app = FastAPI(title="copperline-ai-gw", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.usage = usage
    app.state.model = model or (StubModel(settings.stub_replies_path) if settings.stub_model else
                                BedrockModel(settings.aws_region, settings.route_models,
                                             timeout_s=settings.bedrock_timeout_s))
    setup_service(app, SERVICE)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_api_route("/v1/converse", converse, methods=["POST"])
    return app


def create_admin_app(settings: Settings, usage: Usage) -> FastAPI:
    """The presenter's view of gateway usage. No auth: its only protection is the loopback-only port mapping."""
    app = FastAPI(title="copperline-ai-gw-admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.usage = usage
    setup_service(app, ADMIN_SERVICE)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_api_route("/v1/usage", get_usage, methods=["GET"])
    app.add_api_route("/v1/usage/reset", reset_usage, methods=["POST"])
    return app


async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return error(exc.status_code, HTTP_ERROR_CODES.get(exc.status_code, "error"), str(exc.detail))


# ---- /v1/converse ---------------------------------------------------------------------------------------------------


async def converse(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    usage: Usage = request.app.state.usage
    agent_id = request.headers.get("x-agent-id")
    obo_sub = request.headers.get("x-obo-sub")
    scenario = request.headers.get("x-demo-scenario")
    started = time.monotonic()

    def refuse(status: int, code: str, message: str, route: str | None = None) -> JSONResponse:
        log_event("converse_refused", level="warning", status=status, error=code, reason=message, route=route,
                  agent_id_header=agent_id, obo_sub_header=obo_sub)
        return error(status, code, message)

    if settings.require_key and not _key_ok(request, settings.api_key):
        return refuse(401, "invalid_api_key", "missing or wrong x-api-key")

    routes = request.headers.getlist(ROUTE_HEADER)
    if len(routes) != 1:
        # XC llm-lb's policy matches this header; never act on a copy it may not have judged (docs/08 R37).
        return refuse(400, "bad_route", f"send exactly one {ROUTE_HEADER} header (got {len(routes)})")
    route = routes[0]
    if route not in ROUTES or route in settings.unknown_routes:
        return refuse(400, "bad_route", f"unknown route {route!r}", route)

    try:
        payload = await request.json()
    except ValueError:
        return refuse(400, "bad_request", "body is not JSON", route)
    body, problem = _checked_body(payload, settings.max_tokens_cap)
    if problem:
        return refuse(400, "bad_request", problem, route)

    if usage.over_budget(route):
        return refuse(429, "budget_exceeded", f"daily token budget for route {route!r} is used up", route)

    try:
        # boto3 is blocking; keep it off the event loop.
        result = await run_in_threadpool(request.app.state.model.converse, route, body, scenario)
    except ModelError as e:
        return refuse(e.status, e.error, e.message, route)

    tokens = result["usage"]
    usage.record(route, agent_id, tokens["inputTokens"], tokens["outputTokens"])
    log_event("converse", status=200, latency_ms=int((time.monotonic() - started) * 1000), route=route,
              model_id=result["model_id"], scenario=scenario, agent_id_header=agent_id, obo_sub_header=obo_sub,
              tokens_in=tokens["inputTokens"], tokens_out=tokens["outputTokens"], stop_reason=result["stop_reason"])
    return JSONResponse(result)


def _key_ok(request: Request, expected: str | None) -> bool:
    given = request.headers.getlist(API_KEY_HEADER)
    return bool(expected) and len(given) == 1 and hmac.compare_digest(given[0].encode(), expected.encode())


def _checked_body(payload, max_tokens_cap: int) -> tuple[dict, str | None]:
    """The parts of the body we pass on, or a reason it is unusable. Deliberately shallow: Bedrock validates the
    content blocks in detail, and the stub only reads text, toolUse and toolResult."""
    if not isinstance(payload, dict):
        return {}, "body must be a JSON object"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return {}, "messages must be a non-empty list"
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            return {}, "each message needs role user or assistant"
        content = message.get("content")
        if not isinstance(content, list) or not content or not all(isinstance(b, dict) for b in content):
            return {}, "each message needs a non-empty content list of blocks"
    if messages[0]["role"] != "user":
        return {}, "the first message must be from the user"
    system = payload.get("system")
    if system is not None and not isinstance(system, str):
        return {}, "system must be a string"
    tools = payload.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(
            isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("input_schema"), dict)
            for t in tools
        ):
            return {}, "tools must be a list of {name, description, input_schema}"
    max_tokens = payload.get("max_tokens", max_tokens_cap)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        return {}, "max_tokens must be a positive integer"
    body = {"messages": messages, "max_tokens": min(max_tokens, max_tokens_cap)}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    return body, None


# ---- admin ----------------------------------------------------------------------------------------------------------


async def get_usage(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    snapshot = request.app.state.usage.snapshot()
    snapshot["mode"] = "stub" if settings.stub_model else "bedrock"
    return snapshot


async def reset_usage(request: Request) -> dict:
    request.app.state.usage.reset()
    log_event("usage_reset", status=200, request_id_header=request.headers.get(REQUEST_ID_HEADER))
    return {"status": "reset"}
