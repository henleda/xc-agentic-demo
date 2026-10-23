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
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from copperline_common import demo_driver
from copperline_common.personas import Personas
from copperline_common.scenarios import PlanError, Scenario, expand_plan, load_scenarios

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_env  # noqa: E402

ROOT = demo_env.ROOT
LAST = ("S4", "S6")  # rate-limited scenarios run last
RATE_LIMIT_WAIT_S = 65
JOB_TIMEOUT_S = 300


# ---- driving one scenario: copperline_common.demo_driver, shared with the cockpit ------------------------------


def compose_job(name: str) -> tuple[int, str, str]:
    """S5 locally: the compose batch job (deploy/local/compose.sh run --rm <name>). The cockpit uses the same runner
    locally; on EC2 it creates a vK8s Job instead (Phase 2)."""
    try:
        done = subprocess.run([str(ROOT / "deploy/local/compose.sh"), "run", "--rm", "-T", name],
                              capture_output=True, text=True, timeout=JOB_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        partial = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, partial, f"the job did not finish within {JOB_TIMEOUT_S} s"
    return done.returncode, done.stdout, done.stderr


def run_one(scenario: Scenario, env: demo_env.DemoEnv, personas: Personas, run_salt: str) -> dict:
    """What happened (never raises); see copperline_common.demo_driver.run_one."""
    job_runner = compose_job if env.name == "local" else None
    outcome = demo_driver.run_one(scenario, env, personas, run_salt, run_job=job_runner)
    if job_runner is None and scenario.persona.startswith("job:"):
        outcome["error"] = "S5 in the tenant runs through the cockpit (a Job from the CronJob): Phase 2"
    return outcome


def check(scenario: Scenario, env_name: str, outcome: dict, plan_env: dict | None = None) -> dict:
    return demo_driver.judge(scenario, env_name, outcome, plan_env)


# Kept as names here so the review tests (and readers) find them where they always were.
front_door_row = demo_driver.front_door_row
estate = demo_driver.estate
wrong_run = demo_driver.wrong_run
ESTATE_KEYS = demo_driver.ESTATE_KEYS


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
            job_env = demo_env.job_env(env) if scenario.persona.startswith("job:") else None
            result = check(scenario, env.name, outcome, demo_driver.judge_env(env, job_env))
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
