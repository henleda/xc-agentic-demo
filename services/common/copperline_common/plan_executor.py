"""run_plan(): the ONLY implementation of scenario plans (agent-runtime, cockpit and run_scenario use it).

Every call becomes one trace Step. Fabric errors are data: a 401/403/429 is recorded with blocked_by,
never raised. The only exception run_plan raises is PlanError, before any step runs.
"""

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field

import httpx

from copperline_common import credentials
from copperline_common.converse import chat_to_converse, reply_text
from copperline_common.logging import log_event, redact
from copperline_common.mcp import McpClient, tool_result_is_error, tool_result_text
from copperline_common.outbound import HttpResult, send
from copperline_common.requestid import REQUEST_ID_HEADER, new_request_id, request_id_var
from copperline_common.scenarios import PlanError, PlanStep, Scenario, expand_plan
from copperline_common.tokens import client_credentials
from copperline_common.xcblock import classify, model_feedback

PREVIEW_LIMIT = 4000
ARGS_PREVIEW_LIMIT = 300
SUMMARY_RESULT_LIMIT = 1500
MODEL_MAX_TOKENS = 400

TOOLS_AUDIENCE = "copperline-tools"
LLM_AUDIENCE = "copperline-llm"
TOOLS_TOKEN = f"aud:{TOOLS_AUDIENCE}"
LLM_TOKEN = f"aud:{LLM_AUDIENCE}"
SUMMARY_HEADER = "Results of the tool calls you made:"
IDENTITY_CLAIMS = ("agent_id", "agent_role", "obo_sub", "obo_role", "aud", "sub")


@dataclass
class Step:
    """One trace row. Field meanings are in docs/02-agents.md."""

    n: int
    i: int | None
    kind: str
    target: str
    tool: str | None = None
    method: str | None = None  # http steps: the HTTP method (GET, POST, ...)
    mcp_name_header: str | None = None
    args_preview: str | None = None
    status: int | None = None
    blocked_by: str | None = None
    reason: str | None = None
    is_error: bool | None = None
    result_preview: str | None = None
    result_truncated: bool = False
    identity: dict | None = None
    latency_ms: int = 0
    scripted: bool = True
    request_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExecContext:
    http: httpx.AsyncClient
    tools_base_url: str | None
    llm_base_url: str | None
    idp_base_url: str | None
    tokens: dict[str, str]  # saved tokens by name; also "aud:copperline-tools" / "aud:copperline-llm"
    agent_id: str | None = None
    obo_sub: str | None = None
    lb_base_urls: tuple[str, ...] = ()  # targets starting with one of these are via_lb=True
    conversation: list[dict] = field(default_factory=list)  # chat messages [{"role", "content"}]
    system_prompt: str | None = None
    scenario_id: str | None = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    model_route_default: str | None = None
    step_hook: Callable[[Step], None] | None = None  # called after each step completes


@dataclass
class PlanRun:
    steps: list[Step]
    reply: str | None
    stopped_early: bool


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _args_preview(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)[:ARGS_PREVIEW_LIMIT]


def _scrub(text: str | None, secret: str | None) -> str | None:
    if text and secret:
        return text.replace(secret, "***")
    return text


def _set_result_preview(step: Step, text: str | None) -> None:
    if text is None:
        return
    step.result_truncated = len(text) > PREVIEW_LIMIT
    step.result_preview = text[:PREVIEW_LIMIT]


def _apply_http(step: Step, result: HttpResult, *, via_lb: bool) -> None:
    step.status = result.status
    step.latency_ms = result.latency_ms
    step.blocked_by, step.reason = classify(result, via_lb=via_lb)


def _via_lb(ctx: ExecContext, url: str) -> bool:
    """True when url goes through one of our LBs. The context's own tools/llm/idp URLs always count, so a raw
    http step to ${TOOLS_BASE_URL} (S5's probe) is classified like any other tools-lb call."""
    bases = (*ctx.lb_base_urls, ctx.tools_base_url, ctx.llm_base_url, ctx.idp_base_url)
    return any(url.startswith(base) for base in bases if base)


def _identity_headers(ctx: ExecContext, request_id: str) -> dict[str, str]:
    headers = {REQUEST_ID_HEADER: request_id}
    if ctx.agent_id:
        headers["x-agent-id"] = ctx.agent_id
    if ctx.obo_sub:
        headers["x-obo-sub"] = ctx.obo_sub
    return headers


def _split_tool(fq: str) -> tuple[str, str]:
    server, _, tool = fq.partition(".")
    return server, tool


def _token_url(ctx: ExecContext, spec: dict) -> str | None:
    if spec.get("url"):
        return spec["url"]
    if ctx.idp_base_url:
        return f"{ctx.idp_base_url.rstrip('/')}/auth/token"
    return None


# ---------------------------------------------------------------------------------------------
# Plan validation (everything that can be wrong is reported before the first call)
# ---------------------------------------------------------------------------------------------


def _check_step(step: PlanStep, ctx: ExecContext) -> None:
    spec = step.spec
    where = f"plan step i={step.i}"
    if step.kind == "tool":
        server, tool = _split_tool(str(spec["tool"]))
        if not server or not tool:
            raise PlanError(f"{where}: tool must be '<server>.<tool>', got {spec['tool']!r}")
        if not ctx.tools_base_url:
            raise PlanError(f"{where}: tool step needs tools_base_url")
    elif step.kind == "model":
        if not ctx.llm_base_url:
            raise PlanError(f"{where}: model step needs llm_base_url")
    elif step.kind == "http":
        http = spec["http"]
        if not isinstance(http, dict) or not http.get("method") or not http.get("url"):
            raise PlanError(f"{where}: http step needs method and url")
    elif step.kind == "token":
        token = spec["token"]
        if not isinstance(token, dict) or token.get("grant", "client_credentials") != "client_credentials":
            raise PlanError(f"{where}: only token grant client_credentials is supported")
        if not token.get("client_id") or not token.get("audience"):
            raise PlanError(f"{where}: token step needs client_id and audience")
        if _token_url(ctx, token) is None:
            raise PlanError(f"{where}: token step needs url or idp_base_url")


# ---------------------------------------------------------------------------------------------
# One function per step kind
# ---------------------------------------------------------------------------------------------


async def _run_token(step: PlanStep, ctx: ExecContext, request_id: str) -> Step:
    spec = step.spec["token"]
    url = _token_url(ctx, spec)
    client_id = spec["client_id"]
    audience = spec["audience"]
    # The preview is built from the plan spec, which never holds the secret (that comes from env).
    trace = Step(n=0, i=step.i, kind="token", target=url, args_preview=_args_preview(spec), request_id=request_id)

    # From the plan's env first (ctx.env, which defaults to os.environ), so a caller can hand a plan a secret without
    # writing it into its own process environment (the cockpit runs S6 many times in one long-lived process).
    secret = ctx.env.get(credentials.secret_env_name(client_id)) or credentials.client_secret(client_id)
    # client_credentials() takes no headers: the step's request ID reaches the IdP through the
    # x-request-id hook of new_http_client(), which reads request_id_var (set in _run_step).
    result = await client_credentials(ctx.http, url, client_id, secret, audience)
    _apply_http(trace, result.http, via_lb=_via_lb(ctx, url))
    if result.token:
        if spec.get("save_as"):
            ctx.tokens[spec["save_as"]] = result.token
        ctx.tokens[f"aud:{audience}"] = result.token
        trace.identity = {key: result.claims[key] for key in IDENTITY_CLAIMS if key in result.claims}
    body = result.http.json
    # The access token itself is a credential, so the JSON preview is redacted like a log line.
    preview = json.dumps(redact(body)) if isinstance(body, dict) else result.http.body_text or None
    _set_result_preview(trace, _scrub(preview, secret))
    return trace


async def _run_tool(step: PlanStep, ctx: ExecContext, request_id: str) -> Step:
    spec = step.spec
    fq = str(spec["tool"])
    server, tool = _split_tool(fq)
    override = spec.get("header_name_override")
    client = McpClient(ctx.tools_base_url, ctx.http)
    trace = Step(
        n=0,
        i=step.i,
        kind="tool",
        target=client.url(server),
        tool=fq,
        mcp_name_header=override if override and override != tool else None,
        args_preview=_args_preview(spec.get("args") or {}),
        request_id=request_id,
    )
    result = await client.call_tool(
        server,
        tool,
        spec.get("args") or {},
        token=ctx.tokens.get(TOOLS_TOKEN),
        header_name_override=override,
        extra_headers=_identity_headers(ctx, request_id),
    )
    _apply_http(trace, result, via_lb=True)
    trace.is_error = tool_result_is_error(result)
    text = tool_result_text(result)
    _set_result_preview(trace, text if text is not None else result.body_text or None)
    return trace


def tool_summary(earlier: list[Step]) -> str | None:
    """The plain-text summary of earlier tool steps that model calls get (None when there are none)."""
    lines = []
    for step in earlier:
        if step.kind != "tool":
            continue
        if step.blocked_by is None and step.result_preview is not None:
            outcome = step.result_preview[:SUMMARY_RESULT_LIMIT]
        else:
            outcome = model_feedback(step.kind, step.status, step.blocked_by, step.reason)
        status = step.status if step.status is not None else "no response"
        lines.append(f"- {step.tool} -> {status}: {outcome}")
    if not lines:
        return None
    return "\n".join([SUMMARY_HEADER, *lines])


async def _run_model(step: PlanStep, ctx: ExecContext, request_id: str, earlier: list[Step], scenario_id: str):
    route = (step.spec.get("model") or {}).get("route") or ctx.model_route_default
    url = f"{ctx.llm_base_url.rstrip('/')}/v1/converse"
    chat = list(ctx.conversation)
    summary = tool_summary(earlier)
    if summary:
        chat.append({"role": "user", "content": summary})
    body: dict = {"messages": chat_to_converse(chat), "max_tokens": MODEL_MAX_TOKENS}
    if ctx.system_prompt is not None:
        body["system"] = ctx.system_prompt
    trace = Step(n=0, i=step.i, kind="model", target=url, args_preview=_args_preview(body), request_id=request_id)

    headers = _identity_headers(ctx, request_id)
    headers["x-demo-scenario"] = scenario_id
    if route:
        headers["x-model-route"] = route
    if ctx.tokens.get(LLM_TOKEN):
        headers["Authorization"] = f"Bearer {ctx.tokens[LLM_TOKEN]}"

    result = await send(ctx.http, "POST", url, json=body, headers=headers)
    _apply_http(trace, result, via_lb=True)
    text = reply_text(result.json) if result.status == 200 else None
    _set_result_preview(trace, text if text is not None else result.body_text or None)
    return trace, text


async def _run_http(step: PlanStep, ctx: ExecContext, request_id: str) -> Step:
    spec = step.spec["http"]
    method = str(spec["method"]).upper()
    url = str(spec["url"])
    trace = Step(n=0, i=step.i, kind="http", target=url, method=method, args_preview=_args_preview(spec.get("body")),
                 request_id=request_id)

    headers = {REQUEST_ID_HEADER: request_id, **(spec.get("headers") or {})}
    auth = spec.get("auth")
    if auth:
        if auth not in ctx.tokens:
            trace.reason = f"no token saved as {auth}"
            return trace
        headers["Authorization"] = f"Bearer {ctx.tokens[auth]}"

    kwargs = {"headers": headers}
    if "body" in spec:
        kwargs["json"] = spec["body"]
    result = await send(ctx.http, method, url, **kwargs)
    _apply_http(trace, result, via_lb=_via_lb(ctx, url))
    _set_result_preview(trace, result.body_text or None)
    return trace


async def _run_step(step: PlanStep, ctx: ExecContext, scenario_id: str, earlier: list[Step]):
    """Returns (trace Step, model reply text or None). Runs as its own asyncio task (see run_plan)."""
    request_id = new_request_id(scenario_id)
    reset = request_id_var.set(request_id)
    try:
        if step.kind == "token":
            return await _run_token(step, ctx, request_id), None
        if step.kind == "tool":
            return await _run_tool(step, ctx, request_id), None
        if step.kind == "model":
            return await _run_model(step, ctx, request_id, earlier, scenario_id)
        return await _run_http(step, ctx, request_id), None
    finally:
        request_id_var.reset(reset)


# ---------------------------------------------------------------------------------------------
# The plan loop
# ---------------------------------------------------------------------------------------------


def _batches(steps: list[PlanStep]) -> list[list[PlanStep]]:
    """Consecutive steps of one repeat group with concurrency > 1 are batched; everything else runs alone."""
    batches: list[list[PlanStep]] = []
    for step in steps:
        last = batches[-1] if batches else None
        if last and step.concurrency > 1 and last[0].group == step.group and len(last) < step.concurrency:
            last.append(step)
        else:
            batches.append([step])
    return batches


def _notify(ctx: ExecContext, scenario_id: str, step: Step) -> None:
    log_event(
        "plan_step",
        scenario=scenario_id,
        i=step.i,
        kind=step.kind,
        target=step.target,
        tool=step.tool,
        status=step.status,
        blocked_by=step.blocked_by,
        latency_ms=step.latency_ms,
        step_request_id=step.request_id,
    )
    if ctx.step_hook is None:
        return
    try:
        ctx.step_hook(step)
    except Exception as e:  # a broken UI hook must not break a live demo
        log_event("step_hook_error", level="warning", error=str(e), step_i=step.i)


async def run_plan(scenario: Scenario, ctx: ExecContext, *, start_n: int = 1) -> PlanRun:
    plan = expand_plan(scenario.plan, ctx.env)
    for planned in plan:
        _check_step(planned, ctx)

    executed: list[Step] = []
    last_model_text: str | None = None
    stopped_early = False
    next_n = start_n
    for batch in _batches(plan):
        outcomes = await asyncio.gather(*(_run_step(step, ctx, scenario.id, executed) for step in batch))
        for trace, model_text in sorted(outcomes, key=lambda outcome: outcome[0].i):
            trace.n = next_n
            next_n += 1
            executed.append(trace)
            last_model_text = model_text if trace.kind == "model" else None
            _notify(ctx, scenario.id, trace)
        stop_on = batch[0].stop_on_status
        if stop_on is not None and any(trace.status == stop_on for trace, _ in outcomes):
            stopped_early = True
            break

    if isinstance(scenario.final_reply, str):
        reply = scenario.final_reply
    else:
        reply = last_model_text or None
    return PlanRun(steps=executed, reply=reply, stopped_early=stopped_early)
