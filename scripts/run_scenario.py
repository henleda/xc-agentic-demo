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
the next run (scenarios/README.md, "Repeatability"). A scenario with no expectations for this environment is
reported as "no expectations", never as a pass. Exit code: 0 if every scenario with expectations passed, else 1.

Results: results/scenarios-<UTC timestamp>.json (gitignored), one entry per scenario per run, with the trace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
CHAT_TIMEOUT_S = 600  # a whole agent turn; the front door waits about as long (docs/08 R41)
TRACE_PREFIX = "TRACE "


# ---- driving one scenario ------------------------------------------------------------------------------------------


def run_one(scenario: Scenario, env: demo_env.DemoEnv, personas: Personas) -> dict:
    """{trace, estate_delta, reply, error}: what happened. Never raises for HTTP problems; those are in the trace."""
    if scenario.persona.startswith("job:"):
        return run_job(scenario, env)
    if scenario.persona.startswith("partner:"):
        return asyncio.run(run_partner(scenario, env))
    return run_chat(scenario, env, personas)


def run_chat(scenario: Scenario, env: demo_env.DemoEnv, personas: Personas) -> dict:
    persona = personas.get(scenario.persona)
    door = env.front_doors[env.front_door_for(persona["role"])]
    with httpx.Client(timeout=CHAT_TIMEOUT_S) as client:
        login = client.post(f"{door}/auth/demo-login", json={"sub": scenario.persona,
                                                             "run_salt": os.environ.get("RUN_SALT", "")})
        if login.status_code != 200:
            return {"trace": [], "error": f"login as {scenario.persona} failed: HTTP {login.status_code}"}
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        body = {"messages": [{"role": "user", "content": scenario.message}], "scenario_hint": scenario.id}
        url = f"{door}{demo_env.CHAT_PATH[scenario.agent]}"
        before = estate(client, env)
        if scenario.repeat_message:  # S7: the cockpit sends the message N times; record each front-door answer
            trace = []
            for attempt in range(1, scenario.repeat_message + 1):
                response = client.post(url, json=body, headers=auth)
                trace.append(front_door_row(attempt, response))
            return {"trace": trace, "estate_delta": delta(before, estate(client, env)), "reply": None}
        response = client.post(url, json=body, headers=auth)
        after = estate(client, env)
    if response.status_code != 200:
        return {"trace": [front_door_row(1, response)], "estate_delta": delta(before, after),
                "error": f"chat answered HTTP {response.status_code}"}
    payload = response.json()
    return {"trace": payload["trace"], "estate_delta": delta(before, after), "reply": payload.get("reply"),
            "matched": payload.get("scenario")}


def front_door_row(attempt: int, response: httpx.Response) -> dict:
    """One S7-style row: what the front door answered to one chat attempt."""
    result = HttpResult(status=response.status_code, headers={k.lower(): v for k, v in response.headers.items()},
                        body_text=response.text, json=None, latency_ms=int(response.elapsed.total_seconds() * 1000))
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
                              capture_output=True, text=True, timeout=300)
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


def estate(client: httpx.Client, env: demo_env.DemoEnv) -> dict | None:
    """ERP write counters, or None where the admin port is not reachable (tenant, or ERP down)."""
    if not env.erp_admin_url:
        return None
    try:
        return client.get(f"{env.erp_admin_url}/admin/state", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return None


def delta(before: dict | None, after: dict | None) -> dict | None:
    if before is None or after is None:
        return None
    return {key: after[key] - before[key] for key in ("refunds_issued", "stock_writes")}


# ---- checking and reporting ----------------------------------------------------------------------------------------


def check(scenario: Scenario, env_name: str, outcome: dict) -> dict:
    block = select_block(scenario.expect, env_name)
    result = evaluate(block, outcome.get("trace", []), estate_delta=outcome.get("estate_delta"))
    if result.skipped:
        verdict = "no expectations"
    elif outcome.get("error") and not outcome.get("trace"):
        verdict = "error"
    else:
        verdict = "pass" if result.ok else "fail"
    return {
        "scenario": scenario.id,
        "title": scenario.title,
        "verdict": verdict,
        "error": outcome.get("error"),
        "checks": [{"ok": c.ok, "description": c.description, "detail": c.detail} for c in result.checks],
        "estate_delta": outcome.get("estate_delta"),
        "reply": outcome.get("reply"),
        "trace": outcome.get("trace", []),
    }


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
        plan_env = {**os.environ, **env.plan_env}
        if scenario.persona.startswith("job:") and env.name == "local":
            # Locally the job's env comes from compose (EXFIL_TEST_URL is set on the service), not from this shell.
            plan_env.setdefault("EXFIL_TEST_URL", "https://exfil.invalid/rows")
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

    env = demo_env.for_name(args.env)
    scenarios = ordered(load_scenarios(ROOT / "scenarios"), None if args.all else args.ids)
    if args.dry:
        print(f"dry run, env {env.name}:")
        return dry_run(scenarios, env)
    if env.name == "tenant":
        print("--env tenant is not built yet (Phase 2: the cockpit drives tenant runs).", file=sys.stderr)
        return 2

    personas = Personas.load(ROOT / "config/personas.yaml")
    results, last_limited_call = [], None
    for run in range(1, args.runs + 1):
        if run > 1 and last_limited_call is not None:
            wait = RATE_LIMIT_WAIT_S - (time.monotonic() - last_limited_call)
            if wait > 0:
                print(f"waiting {wait:.0f} s for rate-limit windows to clear ...")
                time.sleep(wait)
        print(f"run {run}/{args.runs}, env {env.name}:")
        for scenario in scenarios:
            result = check(scenario, env.name, run_one(scenario, env, personas))
            result["run"] = run
            results.append(result)
            print_result(result)
            if scenario.id in LAST:
                last_limited_call = time.monotonic()

    out = ROOT / "results" / f"scenarios-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps({"env": env.name, "runs": args.runs, "results": results}, indent=1, default=str))
    counted = [r for r in results if r["verdict"] != "no expectations"]
    passed = sum(r["verdict"] == "pass" for r in counted)
    print(f"{passed}/{len(counted)} passed ({len(results) - len(counted)} with no expectations) -> "
          f"{out.relative_to(ROOT)}")
    return 0 if passed == len(counted) else 1


if __name__ == "__main__":
    sys.exit(main())
