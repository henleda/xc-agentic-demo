#!/usr/bin/env python3
"""Run demo scenarios the way they run on stage, check each one's expect block, and write a results file.

Usage:
    uv run python scripts/run_scenario.py --all --env local              # every scenario once (make local-scenarios)
    uv run python scripts/run_scenario.py S1 S3a --env local             # some of them
    uv run python scripts/run_scenario.py --all --env local --runs 3     # the definition-of-done check
    uv run python scripts/run_scenario.py --all --env local --dry        # load and expand every plan; send nothing

How each scenario is driven (the same paths the presenter's cockpit uses):
    agent scenarios (S0-S4, S7)  log in at the front door, POST /v1/chat to the agent with scenario_hint
    S5 (job:<name>)              the shadow batch job: locally `deploy/local/compose.sh run --rm <name>`
    S6 (partner:<client>)        this script plays the partner: the plan runs here, through partner-lb

S4 and S6 run last (rate-limit windows). With --runs N, the runner waits until 65 s after the last S4/S6 call before
the next run, and each run logs in with a fresh RUN_SALT (scenarios/README.md, "Repeatability").

Verdicts:
    PASS   every check of expect.<env> holds, the agent ran the scenario that was asked for, and the scripted steps
           match the plan in scenarios/ (so a stale image cannot pass an edited plan)
    FAIL   it ran, and a check did not hold; each failing check is printed with what the step really got and why
    ERROR  it could not run properly: login failed, a hop was unreachable, the job exited non-zero, the agent ran a
           different plan. Never counted as a pass, whatever the partial trace shows
    --     no expectations for this environment (S7 locally): reported, never counted as a pass
Exit code: 0 only if at least one scenario was checked and every checked scenario passed; else 1.

Results: results/scenarios-<UTC timestamp>.json (gitignored), written after every scenario, so an interrupted run
keeps what it had. On the devbox, pull them with `make devbox-results`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from copperline_common.credentials import secret_env_name
from copperline_common.expect import evaluate, select_block
from copperline_common.outbound import HttpResult, new_http_client
from copperline_common.personas import Personas
from copperline_common.plan_executor import ExecContext, run_plan
from copperline_common.scenarios import PlanError, Scenario, expand_plan, load_scenarios
from copperline_common.xcblock import classify

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_env  # noqa: E402

ROOT = demo_env.ROOT
LAST = ("S4", "S6")  # rate-limited scenarios run last
RATE_LIMIT_WAIT_S = 65
CHAT_TIMEOUT_S = 660  # longer than the front door's 600 s, so its 504 (with a body) arrives first (docs/08 R41)
JOB_TIMEOUT_S = 300
TRACE_PREFIX = "TRACE "


# ---- driving one scenario ------------------------------------------------------------------------------------------


def run_one(scenario: Scenario, env: demo_env.DemoEnv, personas: Personas, run_salt: str) -> dict:
    """{trace, estate_delta, reply, matched, error}: what happened. Never raises: a hop that is down, a timeout, a
    body that is not JSON or a hung job becomes this scenario's error, and the run goes on to the next scenario."""
    try:
        if scenario.persona.startswith("job:"):
            return run_job(scenario, env)
        if scenario.persona.startswith("partner:"):
            return asyncio.run(run_partner(scenario, env))
        return run_chat(scenario, env, personas, run_salt)
    except subprocess.TimeoutExpired:
        return {"trace": [], "error": f"the job did not finish within {JOB_TIMEOUT_S} s"}
    except httpx.TimeoutException as e:
        return {"trace": [], "error": f"timed out: {type(e).__name__} (is an agent turn stuck?)"}
    except httpx.HTTPError as e:
        return {"trace": [], "error": f"unreachable: {type(e).__name__}: {e} (is the stack up? make local-up)"}
    except Exception as e:  # bad JSON, a missing key, an OS error: still this scenario's error, not the run's
        return {"trace": [], "error": f"{type(e).__name__}: {e}"}


def run_chat(scenario: Scenario, env: demo_env.DemoEnv, personas: Personas, run_salt: str) -> dict:
    persona = personas.get(scenario.persona)
    if persona is None:
        return {"trace": [], "error": f"persona {scenario.persona!r} is not in config/personas.yaml"}
    door = env.front_doors[env.front_door_for(persona["role"])]
    with httpx.Client(timeout=CHAT_TIMEOUT_S) as client:
        login = client.post(f"{door}/auth/demo-login", json={"sub": scenario.persona, "run_salt": run_salt})
        if login.status_code != 200:
            return {"trace": [], "error": f"login as {scenario.persona} failed: HTTP {login.status_code}"}
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        body = {"messages": [{"role": "user", "content": scenario.message}], "scenario_hint": scenario.id}
        url = f"{door}{demo_env.CHAT_PATH[scenario.agent]}"
        before = estate(client, env)
        if scenario.repeat_message:  # S7: the cockpit sends the message N times; record each front-door answer
            trace = []
            for attempt in range(1, scenario.repeat_message + 1):
                started = time.monotonic()
                response = client.post(url, json=body, headers=auth)
                trace.append(front_door_row(attempt, response, started))
            return {"trace": trace, "estate_delta": delta(before, estate(client, env)), "reply": None}
        started = time.monotonic()
        response = client.post(url, json=body, headers=auth)
        after = estate(client, env)
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
            "reason": reason}


def run_job(scenario: Scenario, env: demo_env.DemoEnv) -> dict:
    if env.name != "local":
        return {"trace": [], "error": "S5 in the tenant runs through the cockpit (a Job from the CronJob): Phase 2"}
    job = scenario.persona.removeprefix("job:")
    with httpx.Client(timeout=10) as client:
        before = estate(client, env)
        done = subprocess.run([str(ROOT / "deploy/local/compose.sh"), "run", "--rm", "-T", job],
                              capture_output=True, text=True, timeout=JOB_TIMEOUT_S)
        after = estate(client, env)
    trace = [json.loads(line[len(TRACE_PREFIX):]) for line in done.stdout.splitlines() if line.startswith(TRACE_PREFIX)]
    error = None if done.returncode == 0 else f"job exited {done.returncode}: {done.stderr.strip()[-500:]}"
    return {"trace": trace, "estate_delta": delta(before, after), "error": error}


async def run_partner(scenario: Scenario, env: demo_env.DemoEnv) -> dict:
    """The cockpit's Partner tab: this process plays Granite Supply, with its own client secret."""
    client_id = scenario.persona.removeprefix("partner:")
    secret_name = secret_env_name(client_id)  # granite-supply -> GRANITE_SUPPLY_SECRET
    secret = os.environ.get(secret_name) or demo_env.read_secrets().get(secret_name)
    if not secret:
        return {"trace": [], "error": f"no {secret_name}: run scripts/gen_keys.py (or set it in the shell)"}
    os.environ[secret_name] = secret  # the plan's token step reads it from the process env, like the cockpit's
    plan_env = {**os.environ, **env.plan_env}
    async with new_http_client(timeout=30) as http:
        ctx = ExecContext(http=http, tools_base_url=env.tools_base_url, llm_base_url=env.llm_base_url,
                          idp_base_url=env.idp_base_url, tokens={}, scenario_id=scenario.id, env=plan_env,
                          lb_base_urls=(env.front_doors["partner-lb"],))
        run = await run_plan(scenario, ctx)
    return {"trace": [step.to_dict() for step in run.steps]}


ESTATE_KEYS = ("refunds_issued", "stock_writes")


def estate(client: httpx.Client, env: demo_env.DemoEnv) -> dict | None:
    """ERP write counters, or None when they cannot be read (tenant, ERP down, an unexpected answer). None makes an
    estate_delta expectation fail with "estate not observed", never pass by accident."""
    if not env.erp_admin_url:
        return None
    try:
        response = client.get(f"{env.erp_admin_url}/admin/state", timeout=5)
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


# ---- checking and reporting ----------------------------------------------------------------------------------------


def check(scenario: Scenario, env_name: str, outcome: dict, plan_env: dict | None = None) -> dict:
    block = select_block(scenario.expect, env_name)
    result = evaluate(block, outcome.get("trace", []), estate_delta=outcome.get("estate_delta"))
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


def ordered(scenarios: list[Scenario], wanted: list[str] | None) -> list[Scenario]:
    chosen = scenarios if wanted is None else [s for s in scenarios if s.id in wanted]
    missing = sorted(set(wanted or []) - {s.id for s in scenarios})
    if missing:
        raise SystemExit(f"unknown scenario(s): {', '.join(missing)}")
    return [s for s in chosen if s.id not in LAST] + [s for s in chosen if s.id in LAST]


def dry_run(scenarios: list[Scenario], env: demo_env.DemoEnv) -> int:
    """Expand every plan with this environment's variables; report problems; send nothing."""
    failures = 0
    for scenario in scenarios:
        if scenario.persona.startswith("job:"):
            # The job's plan runs inside its own container: check it against that container's env, not this shell's.
            plan_env = demo_env.job_env(env)
        else:
            plan_env = {**os.environ, **env.plan_env}
        try:
            steps = expand_plan(scenario.plan, plan_env)
            print(f"  ok    {scenario.id:4} {len(steps):3} steps  {scenario.title}")
        except PlanError as e:
            failures += 1
            print(f"  FAIL  {scenario.id:4} {e}")
    return 1 if failures else 0


def print_result(result: dict) -> None:
    mark = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "no expectations": "  --"}[result["verdict"]]
    print(f"  {mark:5} {result['scenario']:4} {result['title']}")
    if result["error"]:
        print(f"        {result['error']}")
    for c in result["checks"]:
        if not c["ok"]:
            print(f"        x {c['description']}: {c['detail']}")
    if result["verdict"] in ("fail", "error"):
        # The steps behind the verdict: what each one really got and why (e.g. a refused token before a null status).
        for step in result["trace"]:
            if step.get("kind") == "front_door" or step.get("status") != 200 or step.get("blocked_by"):
                where = f"i={step['i']}" if step.get("i") is not None else f"n={step.get('n', step.get('attempt'))}"
                print(f"          {where:6} {step.get('kind', ''):10} {step.get('status')!s:5} "
                      f"{step.get('blocked_by') or '':8} {step.get('reason') or ''}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="*", help="scenario ids (S0 S3a ...); --all for every one")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--env", required=True, choices=["local", "tenant"])
    parser.add_argument("--runs", type=int, default=1, help="repeat the whole set (default 1)")
    parser.add_argument("--dry", action="store_true", help="expand plans only; send nothing")
    args = parser.parse_args(argv)
    if args.all == bool(args.ids):
        parser.error("give scenario ids or --all, not both")
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    sys.stdout.reconfigure(line_buffering=True)  # over `make devbox-run` (no tty) each line shows as it happens

    env = demo_env.for_name(args.env)
    scenarios = ordered(load_scenarios(ROOT / "scenarios"), None if args.all else args.ids)
    if args.dry:
        print(f"dry run, env {env.name}:")
        return dry_run(scenarios, env)
    if env.name == "tenant":
        print("--env tenant is not built yet (Phase 2: the cockpit drives tenant runs).", file=sys.stderr)
        return 2

    personas = Personas.load(ROOT / "config/personas.yaml")
    out = ROOT / "results" / f"scenarios-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    results, last_limited_call = [], None
    for run in range(1, args.runs + 1):
        if run > 1 and last_limited_call is not None:
            wait = RATE_LIMIT_WAIT_S - (time.monotonic() - last_limited_call)
            if wait > 0:
                print(f"waiting {wait:.0f} s for rate-limit windows to clear ...")
                time.sleep(wait)
        # A fresh salt per run (Mallory logs in as mallory.x-<salt>), so malicious-user state in XC never carries
        # over between runs. RUN_SALT from the shell is used for run 1 only.
        run_salt = (os.environ.get("RUN_SALT") if run == 1 else None) or secrets.token_hex(4)
        print(f"run {run}/{args.runs}, env {env.name}, run salt {run_salt}:")
        for scenario in scenarios:
            print(f"  ..    {scenario.id:4} running", end="\r")
            outcome = run_one(scenario, env, personas, run_salt)
            result = check(scenario, env.name, outcome, {**os.environ, **env.plan_env})
            result.update(run=run, run_salt=run_salt)
            results.append(result)
            print_result(result)
            write_results(out, env.name, args.runs, results)  # after every scenario: an interrupt keeps the rest
            if scenario.id in LAST:
                last_limited_call = time.monotonic()

    counted = [r for r in results if r["verdict"] != "no expectations"]
    passed = sum(r["verdict"] == "pass" for r in counted)
    print(f"{passed}/{len(counted)} passed ({len(results) - len(counted)} with no expectations) -> "
          f"{out.relative_to(ROOT)}")
    return 0 if counted and passed == len(counted) else 1


def write_results(path: Path, env_name: str, runs: int, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"env": env_name, "runs": runs, "results": results}, indent=1, default=str))
    temp.replace(path)


if __name__ == "__main__":
    sys.exit(main())
