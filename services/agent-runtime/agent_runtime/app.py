"""agent-runtime server mode: POST /v1/chat behind shop-lb or ops-lb. Contract: docs/02-agents.md."""

import httpx
from copperline_common.jwt import bearer_token
from copperline_common.logging import log_event
from copperline_common.outbound import new_http_client
from copperline_common.personas import Personas
from copperline_common.scenarios import load_scenarios
from copperline_common.service import setup_service
from copperline_common.tool_schemas import load_tools
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_runtime.agent import CONNECT_TIMEOUT_S, Agent, AuthError
from agent_runtime.settings import Settings

MAX_MESSAGES = 50
MAX_MESSAGE_CHARS = 8000
HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


def build_agent(settings: Settings, *, http=None, model_http=None) -> Agent:
    """Tests pass http clients that route the LB hostnames in-process."""
    return Agent(
        settings,
        tools=load_tools(settings.config_dir / "tools.yaml"),
        scenarios=load_scenarios(settings.scenarios_dir),
        personas=Personas.load(settings.config_dir / "personas.yaml"),
        http=http or new_http_client(timeout=settings.tool_timeout_s),
        # Model calls (and scripted plans, which run on this client) may read for model_timeout_s; connects fail fast.
        model_http=model_http or new_http_client(timeout=httpx.Timeout(settings.model_timeout_s,
                                                                       connect=CONNECT_TIMEOUT_S)),
    )


def create_app(settings: Settings, agent: Agent | None = None) -> FastAPI:
    app = FastAPI(title=f"copperline-agent-{settings.agent.agent_id}", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.agent = agent or build_agent(settings)
    setup_service(app, f"{settings.agent.agent_id}-agent")
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_api_route("/v1/chat", chat, methods=["POST"])
    log_event("agent_ready", agent=settings.agent.agent_id, route=settings.agent.model_route,
              safe_mode=settings.safe_mode, declared_tools=settings.agent.declared_tools,
              scenarios=[s.id for s in app.state.agent.scenarios if s.agent == settings.agent.agent_id])
    return app


async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse({"error": HTTP_ERROR_CODES.get(exc.status_code, "error"), "message": str(exc.detail)},
                        status_code=exc.status_code)


def _bad(message: str) -> JSONResponse:
    return JSONResponse({"error": "bad_request", "message": message}, status_code=400)


async def chat(request: Request) -> JSONResponse:
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        # XC validated the user token at the front door; with none at all there is no human to act for.
        return JSONResponse({"error": "unauthorized", "message": "missing bearer token"}, status_code=401)
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        return _bad("body is not JSON")
    if not isinstance(body, dict):
        return _bad("body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > MAX_MESSAGES:
        return _bad(f"messages must be a list of 1 to {MAX_MESSAGES} messages")
    for message in messages:
        if (not isinstance(message, dict) or message.get("role") not in ("user", "assistant")
                or not isinstance(message.get("content"), str) or len(message["content"]) > MAX_MESSAGE_CHARS):
            return _bad(f"each message needs role user or assistant and a string content of at most "
                        f"{MAX_MESSAGE_CHARS} characters")
        if not _utf8_ok(message["content"]):
            # JSON allows "\ud800", which cannot be sent on; refuse before any plan step has side effects.
            return _bad("message content is not valid UTF-8 text")
    if messages[-1]["role"] != "user":
        return _bad("the last message must be from the user")
    hint = body.get("scenario_hint")
    if hint is not None and not isinstance(hint, str):
        return _bad("scenario_hint must be a string")
    try:
        result = await request.app.state.agent.handle_chat(token, messages, hint)
    except AuthError as e:
        return JSONResponse({"error": "unauthorized", "message": str(e)}, status_code=401)
    return JSONResponse({"reply": result.reply, "scenario": result.scenario, "trace": result.trace})


def _utf8_ok(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
