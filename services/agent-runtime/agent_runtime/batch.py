"""agent-runtime batch mode (insights-nightly, S5): run the job's scenario once, print the trace, exit 0.

The scenario is the one whose persona is `job:<agent_id>`. Its own token steps get the (copied) client-credentials
tokens, so there is no exchange and no human: obo_sub and obo_role are "none". Each trace Step is printed as one
line, `TRACE <json>`, as it completes; the cockpit tails the pod log. Blocked steps are the expected outcome, so the
exit code is 0 whatever the fabric did; only a broken setup (no scenario, a bad plan) exits non-zero.
"""

import json
import os
import sys

from copperline_common.logging import log_event
from copperline_common.outbound import new_http_client
from copperline_common.plan_executor import ExecContext, Step, run_plan
from copperline_common.scenarios import PlanError, load_scenarios

from agent_runtime.settings import Settings

TRACE_PREFIX = "TRACE "


def job_scenario(settings: Settings):
    persona = f"job:{settings.agent.agent_id}"
    matches = [s for s in load_scenarios(settings.scenarios_dir) if s.persona == persona]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one scenario with persona {persona!r}, found {len(matches)}")
    return matches[0]


def print_step(step: Step) -> None:
    sys.stdout.write(TRACE_PREFIX + json.dumps(step.to_dict(), default=str) + "\n")
    sys.stdout.flush()


async def run_batch(settings: Settings, *, http=None) -> int:
    """Returns the process exit code."""
    try:
        scenario = job_scenario(settings)
    except RuntimeError as e:
        log_event("batch_config_error", level="error", reason=str(e))
        return 2
    env = dict(os.environ)
    env.setdefault("TOOLS_BASE_URL", settings.tools_base_url)
    env.setdefault("LLM_BASE_URL", settings.llm_base_url)
    env.setdefault("IDP_BASE_URL", settings.idp_base_url)
    env.update(settings.extra_env)
    client = http or new_http_client(timeout=settings.model_timeout_s)
    ctx = ExecContext(http=client, tools_base_url=settings.tools_base_url, llm_base_url=settings.llm_base_url,
                      idp_base_url=settings.idp_base_url, tokens={}, agent_id=settings.agent.agent_id,
                      obo_sub="none", conversation=[{"role": "user", "content": scenario.message}],
                      system_prompt=settings.agent.system_prompt, scenario_id=scenario.id, env=env,
                      model_route_default=settings.agent.model_route, step_hook=print_step)
    try:
        run = await run_plan(scenario, ctx)
    except PlanError as e:
        log_event("batch_plan_error", level="error", scenario=scenario.id, reason=str(e))
        return 2
    finally:
        if http is None:
            await client.aclose()
    log_event("batch_done", scenario=scenario.id, steps=len(run.steps),
              blocked=sum(1 for s in run.steps if s.blocked_by))
    return 0
