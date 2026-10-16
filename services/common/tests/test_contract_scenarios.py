"""Black-box contract tests for scenarios.py, run against the REAL files in scenarios/.

Source of truth: API.md (scenarios.py) and scenarios/README.md (schema, substitution order, triggers).
"""
from pathlib import Path

import pytest
from copperline_common.personas import Personas
from copperline_common.scenarios import (
    PlanError,
    by_id,
    expand_plan,
    find_scenario,
    load_scenarios,
    substitute_env,
    substitute_n,
)

REPO = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = REPO / "scenarios"
PERSONAS_YAML = REPO / "config" / "personas.yaml"
EXPECTED_IDS = ["S0", "S1", "S1b", "S2", "S3a", "S3b", "S4", "S5", "S6", "S7"]
PARTNER = "http://partner.test"


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(SCENARIOS_DIR)


@pytest.fixture(scope="module")
def personas():
    return Personas.load(PERSONAS_YAML)


def salted(sub):
    return f"{sub}-ab12" if sub == "mallory.x" else sub


# --- loading ---------------------------------------------------------------------------------------


def test_every_real_scenario_file_loads_in_filename_order(scenarios):
    assert [s.id for s in scenarios] == EXPECTED_IDS


def test_s3_file_yields_two_variants_that_inherit_the_title(scenarios):
    s3a, s3b = by_id(scenarios, "S3a"), by_id(scenarios, "S3b")
    assert s3a.persona == "sam.patel" and s3b.persona == "morgan.chen"
    assert s3a.agent == s3b.agent == "refunds"
    title = "On-behalf-of: right agent, wrong human (a) then right human (b)"
    assert s3a.title == s3b.title == title
    assert s3a.expect["local"]["estate_delta"] == {"refunds_issued": 0}
    assert s3b.expect["local"]["estate_delta"] == {"refunds_issued": 1}
    assert "S3-obo.yaml" in s3a.source and "S3-obo.yaml" in s3b.source


def test_scenario_fields_are_loaded(scenarios):
    s0, s1, s7 = by_id(scenarios, "S0"), by_id(scenarios, "S1"), by_id(scenarios, "S7")
    assert s0.persona == "alex.rivera" and s0.agent == "shopper"
    assert s0.trigger == "where is my order CO-1042"
    assert s0.final_reply is None
    assert len(s0.plan) == 3
    assert s1.final_reply.startswith("Here's a summary of the Alpine 2 reviews")
    assert s7.plan == []
    assert s7.repeat_message == 8
    assert s0.repeat_message is None
    assert by_id(scenarios, "S6").final_reply is None   # key absent in the file
    assert "S0-baseline.yaml" in s0.source


def test_by_id_returns_none_for_unknown_id(scenarios):
    assert by_id(scenarios, "S99") is None


def test_default_directory_comes_from_scenarios_dir_env(monkeypatch):
    monkeypatch.setenv("SCENARIOS_DIR", str(SCENARIOS_DIR))
    assert [s.id for s in load_scenarios()] == EXPECTED_IDS


# --- trigger matching ------------------------------------------------------------------------------


def test_s1_trigger_matches_a_salted_mallory(scenarios, personas):
    s1 = by_id(scenarios, "S1")
    assert s1.matches(agent_id="shopper", message=s1.message, sub="mallory.x-ab12", personas=personas)


def test_s0_does_not_match_a_suffixed_alex_because_alex_is_not_salted(scenarios, personas):
    s0 = by_id(scenarios, "S0")
    assert s0.matches(agent_id="shopper", message=s0.message, sub="alex.rivera", personas=personas)
    assert not s0.matches(agent_id="shopper", message=s0.message, sub="alex.rivera-ab12", personas=personas)


def test_trigger_is_case_insensitive_search_anywhere_in_the_message(scenarios, personas):
    s1 = by_id(scenarios, "S1")
    messages = ["SUMMARIZE THE REVIEWS FOR THE ALPINE 2 TENT", "hey, could you summarize the reviews for the Alpine 2?"]
    for message in messages:
        assert s1.matches(agent_id="shopper", message=message, sub="mallory.x-zz99", personas=personas)
    assert not s1.matches(agent_id="shopper", message="summarise the reviews for the alpine 2", sub="mallory.x",
                          personas=personas)


def test_trigger_requires_matching_agent_and_persona(scenarios, personas):
    s1 = by_id(scenarios, "S1")
    assert not s1.matches(agent_id="storeops", message=s1.message, sub="mallory.x", personas=personas)
    assert not s1.matches(agent_id="shopper", message=s1.message, sub="alex.rivera", personas=personas)


def test_s3_trigger_escapes_the_dollar_sign(scenarios, personas):
    s3a = by_id(scenarios, "S3a")
    assert s3a.matches(agent_id="refunds", message="refund $120 on co-1042 please", sub="sam.patel", personas=personas)
    assert not s3a.matches(agent_id="refunds", message="refund 120 on co-1042", sub="sam.patel", personas=personas)


def test_every_scenario_is_found_by_its_own_message_and_persona(scenarios, personas):
    for sc in scenarios:
        found = find_scenario(scenarios, agent_id=sc.agent, message=sc.message, sub=salted(sc.persona),
                              personas=personas)
        assert found is sc, sc.id


def test_find_scenario_picks_the_variant_by_persona(scenarios, personas):
    msg = "Refund $120 on CO-1042, item arrived damaged."
    assert find_scenario(scenarios, agent_id="refunds", message=msg, sub="sam.patel", personas=personas).id == "S3a"
    assert find_scenario(scenarios, agent_id="refunds", message=msg, sub="morgan.chen", personas=personas).id == "S3b"


def test_find_scenario_hint_restricts_to_one_id(scenarios, personas):
    msg = "Refund $120 on CO-1042, item arrived damaged."
    kw = {"agent_id": "refunds", "message": msg, "personas": personas}
    assert find_scenario(scenarios, sub="sam.patel", hint="S3a", **kw).id == "S3a"
    assert find_scenario(scenarios, sub="sam.patel", hint="S3b", **kw) is None
    assert find_scenario(scenarios, sub="sam.patel", hint="S0", **kw) is None


def test_find_scenario_returns_none_without_a_match(scenarios, personas):
    assert find_scenario(scenarios, agent_id="shopper", message="hello there", sub="alex.rivera",
                         personas=personas) is None


# --- substitution ----------------------------------------------------------------------------------


def test_substitute_env_replaces_recursively_and_leaves_other_values():
    value = {"url": "${BASE}/v1/${PATH}", "list": ["${BASE}", 3, None], "n": 7, "keep": "{n} and {\"a\": 1}"}
    out = substitute_env(value, {"BASE": "http://x.test", "PATH": "asn"})
    assert out == {"url": "http://x.test/v1/asn", "list": ["http://x.test", 3, None], "n": 7,
                   "keep": "{n} and {\"a\": 1}"}


@pytest.mark.parametrize("env", [{}, {"MISSING_VAR": ""}])
def test_substitute_env_missing_or_empty_variable_raises_plan_error(env):
    with pytest.raises(PlanError) as exc:
        substitute_env({"url": "${MISSING_VAR}/x"}, env)
    assert "MISSING_VAR" in str(exc.value)


def test_substitute_n_replaces_only_n_placeholders_and_leaves_other_braces():
    value = {
        "store_id": "S-{n:03d}",
        "page": "page={n}",
        "json": '{"a": 1}',
        "empty": "{}",
        "other": "{name} {x:03d}",
        "nested": [{"q": "p{n}"}, 5],
    }
    out = substitute_n(value, 7)
    assert out == {
        "store_id": "S-007",
        "page": "page=7",
        "json": '{"a": 1}',
        "empty": "{}",
        "other": "{name} {x:03d}",
        "nested": [{"q": "p7"}, 5],
    }


# --- plan expansion: real S4 and S6 ----------------------------------------------------------------


def test_real_s4_plan_expands_to_41_steps_with_1_based_store_ids(scenarios):
    steps = expand_plan(by_id(scenarios, "S4").plan, {})
    assert len(steps) == 41
    assert [s.i for s in steps] == list(range(41))
    assert steps[0].kind == "model" and steps[0].n is None and steps[0].group == 0
    for i in range(1, 41):
        step = steps[i]
        assert step.kind == "tool"
        assert step.n == i
        assert step.group == 1
        assert step.concurrency == 1
        assert step.stop_on_status is None
        assert step.spec["tool"] == "inventory.adjust_stock"
        assert step.spec["args"] == {"sku": "TNT-ALPINE-2", "store_id": f"S-{i:03d}", "delta": 2, "reason": "rebalance"}
        assert "repeat" not in step.spec and "args_template" not in step.spec and "concurrency" not in step.spec
    assert steps[1].spec["args"]["store_id"] == "S-001"
    assert steps[40].spec["args"]["store_id"] == "S-040"


def test_real_s6_plan_expands_to_54_steps_with_pages_1_to_50(scenarios):
    steps = expand_plan(by_id(scenarios, "S6").plan, {"PARTNER_BASE_URL": PARTNER})
    assert len(steps) == 54
    assert [s.i for s in steps] == list(range(54))
    assert [s.kind for s in steps[:4]] == ["token", "http", "http", "http"]
    assert steps[0].spec["token"]["url"] == "http://partner.test/auth/token"
    assert steps[2].spec["http"]["url"] == "http://partner.test/v1/internal/cost-sheet"
    assert steps[3].spec["http"]["body"]["po_id"] == "PO-5531' OR '1'='1"
    for s in steps[:4]:
        assert s.concurrency == 1 and s.n is None
    for i in range(4, 54):
        step = steps[i]
        assert step.kind == "http"
        assert step.n == i - 3
        assert step.group == 4
        assert step.concurrency == 10
        assert step.spec["http"]["url"] == f"http://partner.test/v1/purchase-orders?page={i - 3}"
        assert step.spec["http"]["auth"] == "partner_tok"


def test_missing_env_var_raises_plan_error_for_the_real_s6_plan(scenarios):
    with pytest.raises(PlanError) as exc:
        expand_plan(by_id(scenarios, "S6").plan, {})
    assert "PARTNER_BASE_URL" in str(exc.value)


def test_n_is_not_substituted_in_steps_without_repeat():
    steps = expand_plan([{"http": {"method": "GET", "url": "http://x.test/?page={n}"}}], {})
    assert steps[0].n is None
    assert steps[0].spec["http"]["url"] == "http://x.test/?page={n}"


def test_env_is_substituted_before_n_in_repeated_steps():
    plan = [{"http": {"method": "GET", "url": "${BASE}/p/{n}", "body": {"raw": '{"k": "{n}"}'}}, "repeat": 2}]
    steps = expand_plan(plan, {"BASE": "http://b.test"})
    assert [s.spec["http"]["url"] for s in steps] == ["http://b.test/p/1", "http://b.test/p/2"]
    assert steps[1].spec["http"]["body"] == {"raw": '{"k": "2"}'}


def test_stop_on_status_is_carried_on_each_expanded_step():
    plan = [{"tool": "inventory.adjust_stock", "args_template": {"store_id": "S-{n}"}, "repeat": 3,
             "stop_on_status": 429}]
    steps = expand_plan(plan, {})
    assert [s.stop_on_status for s in steps] == [429, 429, 429]
    assert [s.spec["args"]["store_id"] for s in steps] == ["S-1", "S-2", "S-3"]


@pytest.mark.parametrize("bad_step", [
    {"args": {"a": 1}},
    {"tool": "orders.get_order_status", "model": {"route": "fast"}},
    {"http": {"method": "GET", "url": "http://x.test"}, "token": {"client_id": "c", "audience": "a"}},
])
def test_step_without_exactly_one_kind_raises_plan_error(bad_step):
    with pytest.raises(PlanError):
        expand_plan([{"model": {"route": "fast"}}, bad_step], {})


def test_every_real_scenario_plan_expands_with_a_full_env(scenarios):
    env = {"EXFIL_TEST_URL": "http://exfil.test", "TOOLS_BASE_URL": "http://tools.test",
           "PARTNER_BASE_URL": PARTNER, "IDP_BASE_URL": "http://idp.test"}
    sizes = {s.id: len(expand_plan(s.plan, env)) for s in scenarios}
    assert sizes == {"S0": 3, "S1": 3, "S1b": 1, "S2": 2, "S3a": 2, "S3b": 2, "S4": 41, "S5": 6, "S6": 54, "S7": 0}
