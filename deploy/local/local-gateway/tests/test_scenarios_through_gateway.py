"""The real scenario plans, through the real gateway logic, against the real idp, erp-mock and MCP servers.

This is the step-5 checkpoint in-process: allowed calls succeed, S1 / S3a / S5 are denied by the local-gateway
(blocked_by: xc, labelled NOT XC), and S1b passes the gateway and is rejected by the MCP server (blocked_by: server).
Model steps go through llm-lb to ai-gw in stub mode: allowed ones get a canned 200 reply, and S5's premium call is
a gateway 403 (sp-llm) that never reaches ai-gw.
"""

import asyncio
import json
from pathlib import Path

import pytest
from copperline_common.credentials import client_secret
from copperline_common.expect import evaluate, select_block
from copperline_common.personas import Personas
from copperline_common.plan_executor import ExecContext, run_plan
from copperline_common.scenarios import by_id, load_scenarios
from copperline_common.tokens import exchange

REPO = Path(__file__).resolve().parents[4]
TOOLS = "http://tools.agentic-demo"
LLM = "http://llm.agentic-demo"
IDP = "http://idp.agentic-demo"
SCENARIOS = load_scenarios(REPO / "scenarios")
PERSONAS = Personas.load(REPO / "config/personas.yaml")
AGENT_CLIENTS = {"shopper": "agent-shopper", "storeops": "agent-storeops", "refunds": "agent-refunds"}
ENV = {"TOOLS_BASE_URL": TOOLS, "IDP_BASE_URL": IDP, "LLM_BASE_URL": LLM,
       "EXFIL_TEST_URL": "https://exfil.invalid/rows", "PARTNER_BASE_URL": "http://partner-lb"}


async def user_login(stack, sub: str, front_door: str) -> str:
    response = await stack.http.post(f"http://{front_door}/auth/demo-login", json={"sub": sub, "run_salt": "t1"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def agent_tokens(stack, agent: str, user_token: str) -> dict[str, str]:
    """What agent-runtime will do in server mode: exchange the user token through idp-lb, once per audience."""
    tokens = {}
    for audience in ("copperline-tools", "copperline-llm"):
        result = await exchange(stack.http, IDP, client_id=AGENT_CLIENTS[agent],
                                client_secret=client_secret(AGENT_CLIENTS[agent]), subject_token=user_token,
                                requested_audience=audience)
        assert result.token, result.http.body_text
        tokens[f"aud:{audience}"] = result.token
    return tokens


async def run_scenario(stack, scenario_id: str) -> tuple[list[dict], dict]:
    scenario = by_id(SCENARIOS, scenario_id)
    tokens: dict[str, str] = {}
    obo_sub = None
    if not scenario.persona.startswith(("job:", "partner:")):
        front_door = "shop-lb" if PERSONAS.get(scenario.persona)["role"] == "customer" else "ops-lb"
        user_token = await user_login(stack, scenario.persona, front_door)
        tokens = await agent_tokens(stack, scenario.agent, user_token)
        obo_sub = scenario.persona
    before = stack.db.state()
    ctx = ExecContext(http=stack.http, tools_base_url=TOOLS, llm_base_url=LLM, idp_base_url=IDP, tokens=tokens,
                      agent_id=scenario.agent, obo_sub=obo_sub, scenario_id=scenario.id, env=ENV)
    run = await run_plan(scenario, ctx)
    after = stack.db.state()
    delta = {key: after[key] - before[key] for key in ("refunds_issued", "stock_writes")}
    return [step.to_dict() for step in run.steps], delta


@pytest.mark.parametrize("scenario_id", ["S0", "S1", "S1b", "S2", "S3a", "S3b", "S5"])
def test_expect_local_passes_through_the_gateway(stack, scenario_id, capsys):
    trace, delta = asyncio.run(run_scenario(stack, scenario_id))
    block = select_block(by_id(SCENARIOS, scenario_id).expect, "local")
    result = evaluate(block, trace, estate_delta=delta)
    assert result.ok, "\n".join(f"{c.description}: {c.detail}" for c in result.checks if not c.ok) + "\n" + \
        json.dumps(trace, indent=1)[:4000]
    # Defense in depth must never be what stopped the call: the gateway denies first (mcp-servers README).
    assert "server_authz_deny" not in capsys.readouterr().out


def test_s1_and_s3a_denials_are_the_gateway_not_the_server(stack):
    for scenario_id, i in (("S1", 2), ("S3a", 1)):
        trace, _ = asyncio.run(run_scenario(stack, scenario_id))
        step = next(s for s in trace if s["i"] == i)
        assert (step["status"], step["blocked_by"]) == (403, "xc")
        assert step["reason"].startswith("local-gateway (NOT XC)")


def test_s1b_passes_the_gateway_and_the_server_rejects_the_mismatch(stack):
    trace, _ = asyncio.run(run_scenario(stack, "S1b"))
    step = trace[0]
    assert (step["status"], step["blocked_by"]) == (400, "server")
    assert "HeaderMismatch" in step["reason"]


def test_s5_full_trace_shape_locally(stack):
    """Beyond expect.local: machine tokens are issued (0, 1), the tool call, the premium model call and the export
    probe are gateway 403s (2, 3, 5: sp-tools, sp-llm, sp-tools), and exfil fails at the network (4)."""
    trace, _ = asyncio.run(run_scenario(stack, "S5"))
    by_i = {s["i"]: s for s in trace}
    assert by_i[0]["status"] == 200 and by_i[0]["identity"]["obo_role"] == "none"
    assert by_i[1]["status"] == 200
    for i in (2, 3, 5):
        assert (by_i[i]["status"], by_i[i]["blocked_by"]) == (403, "xc"), by_i[i]
        assert by_i[i]["reason"].startswith("local-gateway (NOT XC)")
    assert (by_i[4]["status"], by_i[4]["blocked_by"]) == (None, "network")


def test_s3b_issues_exactly_one_refund_through_both_hops(stack):
    """tools-lb (agent token) then erp-lb (service token): two gateways, both allow."""
    _, delta = asyncio.run(run_scenario(stack, "S3b"))
    assert delta == {"refunds_issued": 1, "stock_writes": 0}


def test_the_idp_lb_does_not_expose_demo_login(stack):
    async def go():
        return await stack.http.post(f"{IDP}/auth/demo-login", json={"sub": "alex.rivera"})
    response = asyncio.run(go())
    assert response.status_code == 403 and response.headers["x-local-gateway"] == "idp-lb"


@pytest.mark.parametrize("scenario_id, i, route", [("S0", 2, "fast"), ("S1", 1, "fast"), ("S2", 1, "standard")])
def test_allowed_model_steps_reach_ai_gw_through_llm_lb(stack, scenario_id, i, route):
    """llm-lb validates the agent token, applies sp-llm, swaps in the gateway key; ai-gw answers with its stub."""
    trace, _ = asyncio.run(run_scenario(stack, scenario_id))
    step = next(s for s in trace if s["i"] == i)
    assert (step["kind"], step["status"], step["blocked_by"]) == ("model", 200, None), step
    assert step["result_preview"].startswith("(stub model)")
    usage = stack.usage.snapshot()
    assert usage["by_route"][route]["calls"] == 1
    agent = by_id(SCENARIOS, scenario_id).agent
    assert usage["by_agent"][agent]["calls"] == 1  # x-agent-id from the agent runtime reached ai-gw


def test_s5_premium_never_reaches_ai_gw(stack):
    asyncio.run(run_scenario(stack, "S5"))
    assert stack.usage.snapshot()["calls"] == 0


def test_agents_cannot_read_gateway_usage_through_llm_lb(stack):
    """sp-llm is pinned to POST /v1/converse (docs/04), so /v1/usage is only on the loopback admin port."""
    async def go():
        user = await user_login(stack, "alex.rivera", "shop-lb")
        token = (await agent_tokens(stack, "shopper", user))["aud:copperline-llm"]
        return await stack.http.get(f"{LLM}/v1/usage", headers={"Authorization": f"Bearer {token}"})
    response = asyncio.run(go())
    assert response.status_code == 403 and response.headers["x-local-gateway"] == "llm-lb"
