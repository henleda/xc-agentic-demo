"""One agent turn. Contract: docs/02-agents.md ("Agent runtime behavior") and services/agent-runtime/README.md.

    handle_chat(user_token, messages, scenario_hint) -> ChatResult(reply, scenario, trace)

1. Exchange the user token at idp-lb for a tools token and an llm token (cached per (obo_sub, aud) until 30 s before
   expiry). Each real exchange is a trace step (kind token, i null); a cache hit adds nothing.
2. Safe mode and a scenario trigger matches: run its plan (copperline_common.plan_executor.run_plan). If the plan
   yields no reply, make one closing model call (i null): no tools, with x-demo-scenario so the stub can answer.
3. Otherwise the normal loop: model call with the declared tools -> run the tool calls it asks for -> feed the
   results back -> repeat until it answers in text or max_steps model calls have been made.

Fabric errors are data (CLAUDE.md rule 9): every call becomes a trace Step classified by xcblock, and a blocked tool
call is fed back to the model as one short line from xcblock.model_feedback, never raised.
"""

import json
import os
import time
from dataclasses import dataclass, field

import httpx
from copperline_common.converse import chat_to_converse, reply_text, tool_specs, tool_uses
from copperline_common.jwt import unverified_claims
from copperline_common.logging import log_event
from copperline_common.mcp import McpClient, tool_result_is_error, tool_result_text
from copperline_common.outbound import send
from copperline_common.personas import Personas
from copperline_common.plan_executor import (
    ARGS_PREVIEW_LIMIT,
    IDENTITY_CLAIMS,
    LLM_AUDIENCE,
    LLM_TOKEN,
    MODEL_MAX_TOKENS,
    PREVIEW_LIMIT,
    TOOLS_AUDIENCE,
    TOOLS_TOKEN,
    ExecContext,
    Step,
    run_plan,
)
from copperline_common.requestid import REQUEST_ID_HEADER, new_request_id, request_id_var
from copperline_common.scenarios import Scenario, find_scenario
from copperline_common.tokens import TokenStore, exchange
from copperline_common.tool_schemas import ToolDef, fq_from_model_name
from copperline_common.xcblock import classify, model_feedback

from agent_runtime.settings import Settings

NO_REPLY = "Sorry, I couldn't complete that request right now."
TOOL_RESULT_LIMIT = 4000  # characters of a tool result handed back to the model


class AuthError(Exception):
    """The caller's user token is missing or unusable (the front door validated it; this only reads it)."""


@dataclass
class ChatResult:
    reply: str
    scenario: str | None
    trace: list[dict] = field(default_factory=list)


class Agent:
    def __init__(self, settings: Settings, *, tools: dict[str, ToolDef], scenarios: list[Scenario],
                 personas: Personas, http: httpx.AsyncClient, model_http: httpx.AsyncClient,
                 token_store: TokenStore | None = None) -> None:
        self.settings = settings
        self.config = settings.agent
        self.scenarios = scenarios
        self.personas = personas
        self.http = http  # tools and idp: short timeout
        self.model_http = model_http  # llm-lb: longer timeout (settings.model_timeout_s)
        self.tokens = token_store or TokenStore()
        # Only the declared tools are offered to the model, the over-declared one included (S1's misconfiguration).
        unknown = [fq for fq in self.config.declared_tools if fq not in tools]
        if unknown:
            raise RuntimeError(f"{self.config.agent_id}: declared tools not in tools.yaml: {', '.join(unknown)}")
        self.declared = [tools[fq] for fq in self.config.declared_tools]

    # ---- the turn -----------------------------------------------------------------------------------------------

    async def handle_chat(self, user_token: str, messages: list[dict], scenario_hint: str | None = None) -> ChatResult:
        claims = unverified_claims(user_token)
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise AuthError("user token has no sub")
        trace: list[Step] = []
        tokens = await self._agent_tokens(user_token, sub, trace)

        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        scenario = None
        if self.settings.safe_mode:
            scenario = find_scenario(self.scenarios, agent_id=self.config.agent_id, message=last_user, sub=sub,
                                     personas=self.personas, hint=scenario_hint)
        if scenario is not None:
            reply = await self._scripted(scenario, messages, tokens, sub, trace)
        else:
            reply = await self._loop(messages, tokens, sub, trace)
        log_event("chat_done", agent=self.config.agent_id, scenario=scenario.id if scenario else None,
                  steps=len(trace), blocked=sum(1 for s in trace if s.blocked_by))
        return ChatResult(reply=reply, scenario=scenario.id if scenario else None,
                          trace=[step.to_dict() for step in trace])

    # ---- tokens -------------------------------------------------------------------------------------------------

    async def _agent_tokens(self, user_token: str, sub: str, trace: list[Step]) -> dict[str, str]:
        """{"aud:copperline-tools": ..., "aud:copperline-llm": ...}; a failed exchange leaves that audience out, and
        the calls that need it then show their own 401 in the trace."""
        tokens: dict[str, str] = {}
        for audience, name in ((TOOLS_AUDIENCE, TOOLS_TOKEN), (LLM_AUDIENCE, LLM_TOKEN)):
            cached = self.tokens.get(sub, audience)
            if cached:
                tokens[name] = cached
                continue
            request_id = new_request_id(self.config.agent_id)
            reset = request_id_var.set(request_id)  # new_http_client() stamps it on the IdP call
            try:
                result = await exchange(self.http, self.settings.idp_base_url, client_id=self.config.client_id,
                                        client_secret=self.settings.client_secret, subject_token=user_token,
                                        requested_audience=audience)
            finally:
                request_id_var.reset(reset)
            step = Step(n=len(trace) + 1, i=None, kind="token", target=f"{self.settings.idp_base_url}/token/exchange",
                        args_preview=json.dumps({"client_id": self.config.client_id, "requested_audience": audience}),
                        scripted=False, request_id=request_id)
            step.status, step.latency_ms = result.http.status, result.http.latency_ms
            step.blocked_by, step.reason = classify(result.http, via_lb=True)
            if result.token:
                step.identity = {key: result.claims[key] for key in IDENTITY_CLAIMS if key in result.claims}
                self.tokens.put(sub, audience, result.token, result.expires_at)
                tokens[name] = result.token
            else:
                step.result_preview = (result.http.body_text or "")[:PREVIEW_LIMIT] or None
            trace.append(step)
        return tokens

    # ---- scripted turn ------------------------------------------------------------------------------------------

    async def _scripted(self, scenario: Scenario, messages: list[dict], tokens: dict[str, str], sub: str,
                        trace: list[Step]) -> str:
        ctx = ExecContext(http=self.model_http, tools_base_url=self.settings.tools_base_url,
                          llm_base_url=self.settings.llm_base_url, idp_base_url=self.settings.idp_base_url,
                          tokens=dict(tokens), agent_id=self.config.agent_id, obo_sub=sub, conversation=messages,
                          system_prompt=self.config.system_prompt, scenario_id=scenario.id,
                          env=self._plan_env(), model_route_default=self.config.model_route)
        run = await run_plan(scenario, ctx, start_n=len(trace) + 1)
        trace.extend(run.steps)
        if run.reply:
            return run.reply
        # No final_reply and the plan did not end in a model step: one closing call, unscripted (i null), no tools.
        summary = list(messages) + [{"role": "user", "content": _plan_summary(run.steps)}]
        step, text = await self._model_call(chat_to_converse(summary), tokens, sub, tools=None, scenario=scenario.id,
                                            n=len(trace) + 1)
        trace.append(step)
        return text or NO_REPLY

    def _plan_env(self) -> dict[str, str]:
        """Env for plan ${VAR} substitution: the process env, plus this agent's LB URLs if the env lacks them."""
        env = dict(os.environ)
        env.setdefault("TOOLS_BASE_URL", self.settings.tools_base_url)
        env.setdefault("LLM_BASE_URL", self.settings.llm_base_url)
        env.setdefault("IDP_BASE_URL", self.settings.idp_base_url)
        env.update(self.settings.extra_env)
        return env

    # ---- normal loop --------------------------------------------------------------------------------------------

    async def _loop(self, messages: list[dict], tokens: dict[str, str], sub: str, trace: list[Step]) -> str:
        conversation = chat_to_converse(messages)
        specs = tool_specs(self.declared)
        for _ in range(self.config.max_steps):
            step, response = await self._model_call(conversation, tokens, sub, tools=specs, scenario=None,
                                                    n=len(trace) + 1, raw=True)
            trace.append(step)
            if step.status != 200 or not isinstance(response, dict):
                return NO_REPLY
            uses = tool_uses(response)
            if not uses:
                return reply_text(response) or NO_REPLY
            conversation.append(response["output"]["message"])
            results = []
            for use in uses:
                tool_step, result_text = await self._tool_call(use, tokens, sub, n=len(trace) + 1)
                trace.append(tool_step)
                results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"text": result_text}],
                                               "status": "error" if tool_step.blocked_by or tool_step.is_error
                                               else "success"}})
            conversation.append({"role": "user", "content": results})
        # max_steps model calls and the model still wants tools: stop and say so rather than loop forever.
        log_event("max_steps_reached", level="warning", agent=self.config.agent_id, max_steps=self.config.max_steps)
        return "I've reached the step limit for this request. Please narrow it down and try again."

    async def _tool_call(self, use: dict, tokens: dict[str, str], sub: str, n: int) -> tuple[Step, str]:
        """Run one tool the model asked for. Returns the trace step and the text to hand back to the model."""
        fq = fq_from_model_name(str(use.get("name", "")))
        server, _, tool = fq.partition(".")
        args = use.get("input") if isinstance(use.get("input"), dict) else {}
        request_id = new_request_id(self.config.agent_id)
        client = McpClient(self.settings.tools_base_url, self.http)
        step = Step(n=n, i=None, kind="tool", target=client.url(server), tool=fq,
                    args_preview=json.dumps(args, default=str)[:ARGS_PREVIEW_LIMIT], scripted=False,
                    request_id=request_id)
        if fq not in self.config.declared_tools:
            # The model named a tool it was never offered: refuse locally, never call it.
            step.reason = "not a declared tool"
            return step, f"Unknown tool {use.get('name')!r}."
        result = await client.call_tool(server, tool, args, token=tokens.get(TOOLS_TOKEN),
                                        extra_headers=self._identity_headers(request_id, sub))
        step.status, step.latency_ms = result.status, result.latency_ms
        step.blocked_by, step.reason = classify(result, via_lb=True)
        step.is_error = tool_result_is_error(result)
        text = tool_result_text(result)
        preview = text if text is not None else (result.body_text or None)
        if preview is not None:
            step.result_truncated = len(preview) > PREVIEW_LIMIT
            step.result_preview = preview[:PREVIEW_LIMIT]
        if step.blocked_by or step.status != 200:
            return step, model_feedback("tool", step.status, step.blocked_by, step.reason)
        return step, (text or "")[:TOOL_RESULT_LIMIT]

    # ---- model calls --------------------------------------------------------------------------------------------

    async def _model_call(self, conversation: list[dict], tokens: dict[str, str], sub: str, *, tools: list | None,
                          scenario: str | None, n: int, raw: bool = False):
        """One POST /v1/converse through llm-lb. Returns (step, text) or, with raw=True, (step, response JSON)."""
        request_id = new_request_id(self.config.agent_id)
        url = f"{self.settings.llm_base_url}/v1/converse"
        body: dict = {"messages": conversation, "max_tokens": MODEL_MAX_TOKENS}
        if self.config.system_prompt:
            body["system"] = self.config.system_prompt
        if tools:
            body["tools"] = tools
        headers = self._identity_headers(request_id, sub)
        headers["x-model-route"] = self.config.model_route
        if scenario:
            headers["x-demo-scenario"] = scenario
        if tokens.get(LLM_TOKEN):
            headers["Authorization"] = f"Bearer {tokens[LLM_TOKEN]}"
        started = time.monotonic()
        result = await send(self.model_http, "POST", url, json=body, headers=headers)
        step = Step(n=n, i=None, kind="model", target=url, scripted=False, request_id=request_id,
                    args_preview=json.dumps({"route": self.config.model_route, "messages": len(conversation),
                                             "tools": len(tools or [])}),
                    status=result.status, latency_ms=result.latency_ms or int((time.monotonic() - started) * 1000))
        step.blocked_by, step.reason = classify(result, via_lb=True)
        text = reply_text(result.json) if result.status == 200 else None
        uses = tool_uses(result.json) if result.status == 200 else []
        preview = text or (json.dumps([u["name"] for u in uses]) if uses else None) or result.body_text or None
        if preview is not None:
            step.result_truncated = len(preview) > PREVIEW_LIMIT
            step.result_preview = preview[:PREVIEW_LIMIT]
        if raw:
            return step, result.json if result.status == 200 else None
        return step, text

    def _identity_headers(self, request_id: str, sub: str) -> dict[str, str]:
        # Informational, for the customer gateway's logs (docs/08 R9); XC judges the token, not these.
        return {REQUEST_ID_HEADER: request_id, "x-agent-id": self.config.agent_id, "x-obo-sub": sub}


def _plan_summary(steps: list[Step]) -> str:
    """What the closing model call is told about a scripted turn: one line per tool call and its outcome."""
    lines = []
    for step in steps:
        if step.kind != "tool":
            continue
        if step.blocked_by or step.status != 200 or step.is_error:
            outcome = model_feedback("tool", step.status, step.blocked_by, step.reason)
        else:
            outcome = (step.result_preview or "")[:1500]
        lines.append(f"- {step.tool} -> {outcome}")
    if not lines:
        return "Please reply to the user."
    return "Results of the tool calls you made:\n" + "\n".join(lines) + "\nNow reply to the user."
