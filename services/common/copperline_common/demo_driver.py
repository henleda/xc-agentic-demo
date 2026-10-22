"""Drive one demo scenario the way the stage does, and judge the result. The ONE implementation, shared by the cockpit
backend (the presenter's buttons) and scripts/run_scenario.py (the acceptance check), so the two never drift apart.

    run_one(scenario, endpoints, personas, run_salt, run_job=...)
        -> outcome {trace, estate_delta, reply, matched, error}
    judge(scenario, env_name, outcome, plan_env) -> result {verdict, error, checks, ...}

How each scenario is driven:
    agent scenarios (S0-S4, S7)  log in at the front door (with the run's salt), POST /v1/chat with scenario_hint;
                                 S7 sends its message repeat_message times and records each front-door answer
    S5 (job:<name>)              the caller's run_job(name) runs the batch job and returns its TRACE output
    S6 (partner:<client>)        this process plays the partner: the plan runs here, through partner-lb

run_one never raises: a hop that is down, a timeout, a body that is not JSON or a hung job becomes the outcome's
`error`. judge() turns any error into ERROR, whatever a partial trace shows.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from copperline_common.credentials import secret_env_name
from copperline_common.expect import evaluate, select_block
from copperline_common.outbound import HttpResult, new_http_client
from copperline_common.personas import Personas
from copperline_common.plan_executor import ExecContext, run_plan
from copperline_common.scenarios import PlanError, Scenario, expand_plan
from copperline_common.xcblock import classify

CHAT_TIMEOUT_S = 660  # longer than the front door's 600 s, so its 504 (with a body) arrives first (docs/08 R41)
TRACE_PREFIX = "TRACE "
ESTATE_KEYS = ("refunds_issued", "stock_writes")
CHAT_PATH = {"shopper": "/v1/chat", "storeops": "/storeops/v1/chat", "refunds": "/refunds/v1/chat"}


@dataclass
class Endpoints:
    """Where the demo is, as seen from the process driving it (the cockpit, or the runner on the same host)."""

    name: str  # "local" or "tenant"
    front_doors: dict[str, str]  # shop-lb / ops-lb / partner-lb -> base URL
    tools_base_url: str | None = None  # internal LBs: only reachable directly in local mode
    llm_base_url: str | None = None
    idp_base_url: str | None = None
    erp_lb_url: str | None = None  # erp-lb: CE-only in the tenant
    erp_admin_url: str | None = None  # loopback admin ports (on copperline-core in the tenant)
    aigw_admin_url: str | None = None
    plan_env: dict[str, str] = field(default_factory=dict)  # ${VAR} values for plans run in this process (S6)
    secrets: dict[str, str] = field(default_factory=dict)  # client secrets this process may use (S6's partner)

    def front_door_for(self, role: str) -> str:
        return "shop-lb" if role == "customer" else "ops-lb"


JobRunner = Callable[[str], tuple[int, str, str]]  # job name -> (exit code, stdout, stderr)


# ---- driving --------------------------------------------------------------------------------------------------------


def run_one(scenario: Scenario, endpoints: Endpoints, personas: Personas, run_salt: str,
            run_job: JobRunner | None = None) -> dict:
    """{trace, estate_delta, reply, matched, error}: what happened. Never raises."""
    try:
        if scenario.persona.startswith("job:"):
            return run_batch_job(scenario, endpoints, run_job)
        if scenario.persona.startswith("partner:"):
            return asyncio.run(run_partner(scenario, endpoints))
        return run_chat(scenario, endpoints, personas, run_salt)
    except httpx.TimeoutException as e:
        return {"trace": [], "error": f"timed out: {type(e).__name__} (is an agent turn stuck?)"}
    except httpx.HTTPError as e:
        return {"trace": [], "error": f"unreachable: {type(e).__name__}: {e} (is the stack up?)"}
    except Exception as e:  # bad JSON, a missing key, an OS error, a job timeout: this scenario's error, not a crash
        return {"trace": [], "error": f"{type(e).__name__}: {e}"}


def login(client: httpx.Client, endpoints: Endpoints, sub: str, role: str, run_salt: str) -> tuple[str | None, str]:
    """(access token, problem) for a demo persona, through its front door."""
    door = endpoints.front_doors[endpoints.front_door_for(role)]
    response = client.post(f"{door}/auth/demo-login", json={"sub": sub, "run_salt": run_salt})
    if response.status_code != 200:
        return None, f"login as {sub} failed: HTTP {response.status_code}"
    return response.json()["access_token"], ""


def chat(client: httpx.Client, endpoints: Endpoints, agent: str, role: str, token: str, messages: list[dict],
         scenario_hint: str | None = None) -> tuple[httpx.Response, float]:
    """One chat turn through the front door: (response, monotonic start time)."""
    door = endpoints.front_doors[endpoints.front_door_for(role)]
    body: dict = {"messages": messages}
    if scenario_hint:
        body["scenario_hint"] = scenario_hint
    started = time.monotonic()
    response = client.post(f"{door}{CHAT_PATH[agent]}", json=body, headers={"Authorization": f"Bearer {token}"})
    return response, started


def run_chat(scenario: Scenario, endpoints: Endpoints, personas: Personas, run_salt: str) -> dict:
    persona = personas.get(scenario.persona)
    if persona is None:
        return {"trace": [], "error": f"persona {scenario.persona!r} is not in config/personas.yaml"}
    messages = [{"role": "user", "content": scenario.message}]
    with httpx.Client(timeout=CHAT_TIMEOUT_S) as client:
        token, problem = login(client, endpoints, scenario.persona, persona["role"], run_salt)
        if token is None:
            return {"trace": [], "error": problem}
        before = estate(client, endpoints)
        if scenario.repeat_message:  # S7: the message N times; record each front-door answer
            trace = []
            for attempt in range(1, scenario.repeat_message + 1):
                response, started = chat(client, endpoints, scenario.agent, persona["role"], token, messages,
                                         scenario.id)
                trace.append(front_door_row(attempt, response, started))
            return {"trace": trace, "estate_delta": delta(before, estate(client, endpoints)), "reply": None}
        response, started = chat(client, endpoints, scenario.agent, persona["role"], token, messages, scenario.id)
        after = estate(client, endpoints)
    if response.status_code != 200:
        return {"trace": [front_door_row(1, response, started)], "estate_delta": delta(before, after),
                "error": f"chat answered HTTP {response.status_code}"}
    payload = response.json()
    return {"trace": payload["trace"], "estate_delta": delta(before, after), "reply": payload.get("reply"),
            "matched": payload.get("scenario")}


def front_door_row(attempt: int, response: httpx.Response, started: float) -> dict:
    """One S7-style row: what the front door answered to one chat attempt (started = time.monotonic() before it)."""
    result = HttpResult(status=response.status_code, headers={k.lower(): v for k, v in response.headers.items()},
                        body_text=response.text, json=None, latency_ms=int((time.monotonic() - started) * 1000))
    blocked_by, reason = classify(result, via_lb=True)
    return {"kind": "front_door", "attempt": attempt, "status": response.status_code, "blocked_by": blocked_by,
            "reason": reason, "latency_ms": result.latency_ms}


def run_batch_job(scenario: Scenario, endpoints: Endpoints, run_job: JobRunner | None) -> dict:
    if run_job is None:
        return {"trace": [], "error": "no way to start the batch job here (local: compose; tenant: a vK8s Job)"}
    job = scenario.persona.removeprefix("job:")
    with httpx.Client(timeout=10) as client:
        before = estate(client, endpoints)
        code, stdout, stderr = run_job(job)
        after = estate(client, endpoints)
    trace = [json.loads(line[len(TRACE_PREFIX):]) for line in stdout.splitlines() if line.startswith(TRACE_PREFIX)]
    error = None if code == 0 else f"job exited {code}: {stderr.strip()[-500:]}"
    return {"trace": trace, "estate_delta": delta(before, after), "error": error}


async def run_partner(scenario: Scenario, endpoints: Endpoints) -> dict:
    """The Partner tab: this process plays Granite Supply with its own client secret."""
    secret_name = secret_env_name(scenario.persona.removeprefix("partner:"))  # granite-supply -> GRANITE_SUPPLY_SECRET
    secret = endpoints.secrets.get(secret_name) or os.environ.get(secret_name)
    if not secret:
        return {"trace": [], "error": f"no {secret_name}: run scripts/gen_keys.py (or set it in the environment)"}
    # The secret travels in the plan's env (the token step reads ctx.env first), never into this process's os.environ.
    plan_env = {**os.environ, **endpoints.plan_env, secret_name: secret}
    async with new_http_client(timeout=30) as http:
        ctx = ExecContext(http=http, tools_base_url=endpoints.tools_base_url, llm_base_url=endpoints.llm_base_url,
                          idp_base_url=endpoints.idp_base_url, tokens={}, scenario_id=scenario.id,
                          env=plan_env, lb_base_urls=(endpoints.front_doors["partner-lb"],))
        run = await run_plan(scenario, ctx)
    return {"trace": [step.to_dict() for step in run.steps]}


def estate(client: httpx.Client, endpoints: Endpoints) -> dict | None:
    """ERP write counters, or None when they cannot be read (no admin port, ERP down, an unexpected answer). None
    makes an estate_delta expectation fail with "estate not observed", never pass by accident."""
    if not endpoints.erp_admin_url:
        return None
    try:
        response = client.get(f"{endpoints.erp_admin_url}/admin/state", timeout=5)
        state = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if response.status_code != 200 or not isinstance(state, dict) or not all(
            isinstance(state.get(key), int) for key in ESTATE_KEYS):
        return None
    return state


def delta(before: dict | None, after: dict | None) -> dict | None:
    if before is None or after is None:
        return None
    return {key: after[key] - before[key] for key in ESTATE_KEYS}


# ---- judging --------------------------------------------------------------------------------------------------------


def judge(scenario: Scenario, env_name: str, outcome: dict, plan_env: dict | None = None) -> dict:
    """The verdict on one outcome: pass / fail / error / no expectations (rules in scripts/run_scenario.py)."""
    result = evaluate(select_block(scenario.expect, env_name), outcome.get("trace", []),
                      estate_delta=outcome.get("estate_delta"))
    error = outcome.get("error") or wrong_run(scenario, outcome, plan_env)
    if result.skipped:
        verdict = "no expectations"
    elif error:
        verdict = "error"  # whatever a partial trace shows: a crashed job or a different plan is never a pass
    else:
        verdict = "pass" if result.ok else "fail"
    return {
        "scenario": scenario.id,
        "title": scenario.title,
        "verdict": verdict,
        "error": error,
        "checks": [{"ok": c.ok, "description": c.description, "detail": c.detail} for c in result.checks],
        "estate_delta": outcome.get("estate_delta"),
        "reply": outcome.get("reply"),
        "trace": outcome.get("trace", []),
    }


def wrong_run(scenario: Scenario, outcome: dict, plan_env: dict | None) -> str | None:
    """Why this trace is not a run of this scenario, or None. Catches an agent that matched a different scenario (or
    none) and a container that runs a different plan than scenarios/ says (a stale image after an edit)."""
    if "matched" in outcome and outcome["matched"] != scenario.id:
        return (f"the agent ran {outcome['matched'] or 'its normal loop (no scenario)'}, not {scenario.id}: "
                "check the persona, the message and the trigger")
    if plan_env is None or not scenario.plan:
        return None
    # Compared per step: plan index, kind, and for tool steps the tool name. (Argument values are not compared: the
    # trace keeps only a 300-character preview of them.)
    try:
        planned = [(step.i, step.kind, step.spec.get("tool")) for step in expand_plan(scenario.plan, plan_env)]
    except PlanError:
        return None  # --dry reports plan problems; here the trace speaks for itself
    ran = [(step["i"], step["kind"], step.get("tool")) for step in outcome.get("trace", [])
           if step.get("scripted") and step.get("i") is not None]
    if ran and ran != planned[:len(ran)]:
        return (f"the trace does not follow scenarios/{scenario.source or scenario.id}: the container runs another "
                "version of the plan (rebuild: make local-up)")
    return None
