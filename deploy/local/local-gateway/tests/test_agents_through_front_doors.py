"""The agents end to end, in-process, the way the cockpit drives them: log in at the front door, then POST /v1/chat
through shop-lb or ops-lb. The agent exchanges the user token itself (through idp-lb), runs the scripted plan or its
normal loop, and returns {reply, scenario, trace}. Each scenario's expect.local is checked against that trace.
"""

import asyncio
from pathlib import Path

import pytest
from ai_gw.models import StubModel
from copperline_common.expect import evaluate, select_block
from copperline_common.personas import Personas
from copperline_common.scenarios import by_id, load_scenarios

REPO = Path(__file__).resolve().parents[4]
SCENARIOS = load_scenarios(REPO / "scenarios")
PERSONAS = Personas.load(REPO / "config/personas.yaml")
CHAT_PATH = {"shopper": "/v1/chat", "storeops": "/storeops/v1/chat", "refunds": "/refunds/v1/chat"}


def front_door(sub: str) -> str:
    return "shop-lb" if PERSONAS.get(sub)["role"] == "customer" else "ops-lb"


async def login(stack, sub: str, run_salt: str = "t1") -> str:
    url = f"http://{front_door(sub)}/auth/demo-login"
    response = await stack.http.post(url, json={"sub": sub, "run_salt": run_salt})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def chat(stack, sub: str, agent: str, message: str, *, token: str | None = None, hint: str | None = None):
    token = token or await login(stack, sub)
    body = {"messages": [{"role": "user", "content": message}]}
    if hint:
        body["scenario_hint"] = hint
    return await stack.http.post(f"http://{front_door(sub)}{CHAT_PATH[agent]}", json=body,
                                 headers={"Authorization": f"Bearer {token}"})


async def run_scenario_chat(stack, scenario_id: str):
    scenario = by_id(SCENARIOS, scenario_id)
    before = stack.db.state()
    response = await chat(stack, scenario.persona, scenario.agent, scenario.message, hint=scenario.id)
    after = stack.db.state()
    assert response.status_code == 200, response.text
    delta = {key: after[key] - before[key] for key in ("refunds_issued", "stock_writes")}
    return response.json(), delta


@pytest.mark.parametrize("scenario_id", ["S0", "S1", "S1b", "S2", "S3a", "S3b", "S4"])
def test_expect_local_passes_through_the_agent(stack, scenario_id, capsys):
    body, delta = asyncio.run(run_scenario_chat(stack, scenario_id))
    assert body["scenario"] == scenario_id
    result = evaluate(select_block(by_id(SCENARIOS, scenario_id).expect, "local"), body["trace"], estate_delta=delta)
    assert result.ok, "\n".join(f"{c.description}: {c.detail}" for c in result.checks if not c.ok)
    assert body["reply"]
    assert "server_authz_deny" not in capsys.readouterr().out  # the fabric stand-in denied first


def test_the_agent_exchanges_tokens_once_per_audience_and_caches_them(stack):
    async def go():
        token = await login(stack, "alex.rivera")
        first = await chat(stack, "alex.rivera", "shopper", "Where is my order CO-1042?", token=token)
        second = await chat(stack, "alex.rivera", "shopper", "Where is my order CO-1042?", token=token)
        return first.json()["trace"], second.json()["trace"]
    first, second = asyncio.run(go())
    token_steps = [s for s in first if s["kind"] == "token"]
    assert [(s["i"], s["status"], s["identity"]["aud"]) for s in token_steps] == [
        (None, 200, "copperline-tools"), (None, 200, "copperline-llm")]
    identity = token_steps[0]["identity"]
    assert (identity["obo_sub"], identity["agent_id"]) == ("alex.rivera", "shopper")
    assert not [s for s in second if s["kind"] == "token"]  # cache hit: no exchange, no trace row


def test_scripted_steps_are_marked_scripted_and_the_closing_call_is_not(stack):
    """S3a has no final_reply and ends on a tool step, so the reply comes from one unscripted closing call."""
    body, _ = asyncio.run(run_scenario_chat(stack, "S3a"))
    plan_steps = [s for s in body["trace"] if s["i"] is not None]
    closing = body["trace"][-1]
    assert plan_steps and all(s["scripted"] for s in plan_steps)
    assert (closing["kind"], closing["i"], closing["scripted"], closing["status"]) == ("model", None, False, 200)
    # The stub's S3a reply (not its default), so the closing call carried x-demo-scenario through llm-lb.
    assert body["reply"] == closing["result_preview"] == StubModel().scenarios["S3a"]


def test_trace_n_is_the_display_order_across_token_plan_and_closing_steps(stack):
    body, _ = asyncio.run(run_scenario_chat(stack, "S3a"))
    assert [s["n"] for s in body["trace"]] == list(range(1, len(body["trace"]) + 1))


def test_a_message_matching_no_trigger_runs_the_normal_loop(stack):
    """Unscripted: the model (stub) asks for orders__get_order_status, the agent runs it, then the model answers."""
    response = asyncio.run(chat(stack, "alex.rivera", "shopper", "What is the status of my order?"))
    body = response.json()
    assert body["scenario"] is None
    kinds = [(s["kind"], s["scripted"]) for s in body["trace"] if s["kind"] != "token"]
    assert kinds == [("model", False), ("tool", False), ("model", False)]
    tool = next(s for s in body["trace"] if s["kind"] == "tool")
    assert tool["tool"] == "orders.get_order_status" and tool["status"] == 200
    assert body["reply"]


def test_a_tool_the_fabric_denies_is_fed_back_and_the_turn_still_ends_well(stack):
    """Rule 9: the shopper's model asks for the over-declared refunds tool; tools-lb denies it; the model is told
    in one line and the agent still replies."""
    response = asyncio.run(chat(stack, "alex.rivera", "shopper", "Please issue a refund for my damaged tent."))
    body = response.json()
    tool = next(s for s in body["trace"] if s["kind"] == "tool")
    assert (tool["tool"], tool["status"], tool["blocked_by"]) == ("refunds.issue_refund", 403, "xc")
    assert response.status_code == 200 and body["reply"]
    assert stack.db.state()["refunds_issued"] == 0


def test_the_wrong_human_is_refused_at_the_token_exchange(stack):
    """A customer token sent to the ops door for storeops: ops-lb's policy refuses it before the agent sees it."""
    async def go():
        token = await login(stack, "alex.rivera")
        return await stack.http.post("http://ops-lb/storeops/v1/chat",
                                     json={"messages": [{"role": "user", "content": "hi"}]},
                                     headers={"Authorization": f"Bearer {token}"})
    response = asyncio.run(go())
    assert response.status_code == 401 and response.headers["x-local-gateway"] == "ops-lb"  # aud copperline-shop


def test_salted_mallory_still_triggers_her_scenarios(stack):
    """The run salt makes mallory.x-<salt>; triggers match on the base sub (plan B1)."""
    async def go():
        token = await login(stack, "mallory.x", run_salt="abc123")
        return await chat(stack, "mallory.x", "shopper", by_id(SCENARIOS, "S1").message, token=token)
    body = asyncio.run(go()).json()
    assert body["scenario"] == "S1"
    assert next(s for s in body["trace"] if s["kind"] == "token")["identity"]["obo_sub"] == "mallory.x-abc123"


def test_another_persona_does_not_trigger_mallorys_scenario(stack):
    body = asyncio.run(chat(stack, "alex.rivera", "shopper", by_id(SCENARIOS, "S1").message)).json()
    assert body["scenario"] is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"messages": []},
        {"messages": [{"role": "user", "content": 5}]},
        {"messages": [{"role": "assistant", "content": "hi"}]},
        {"messages": [{"role": "user", "content": "hi"}], "scenario_hint": 3},
    ],
)
def test_bad_chat_bodies_are_400_from_the_agent(stack, body):
    async def go():
        token = await login(stack, "alex.rivera")
        return await stack.http.post("http://shop-lb/v1/chat", json=body, headers={"Authorization": f"Bearer {token}"})
    response = asyncio.run(go())
    assert response.status_code == 400 and response.headers["x-copperline-origin"] == "shopper-agent"
