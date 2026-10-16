"""Black-box contract tests for expect.py.

Source of truth: scenarios/README.md "Assertion semantics" plus the four details in API.md (expect.py).
The last section evaluates every REAL scenario's expect blocks against a synthetic passing trace, which
proves each block is satisfiable.
"""
from pathlib import Path

import pytest
from copperline_common.expect import Check, ExpectResult, evaluate, select_block
from copperline_common.scenarios import load_scenarios

REPO = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = REPO / "scenarios"
CARD = "4111111111111111"
CARD_SPACED = "4111 1111 1111 1111"


def step(i, kind="tool", status=200, blocked_by=None, *, is_error=None, result_preview="ok", result_truncated=False,
         tool=None):
    """One trace row shaped like plan_executor.Step.to_dict(). `n` is filled in by trace()."""
    if kind == "tool" and is_error is None and status == 200:
        is_error = False
    return {
        "n": 0, "i": i, "kind": kind, "target": f"http://{kind}.test", "tool": tool, "mcp_name_header": None,
        "args_preview": None, "status": status, "blocked_by": blocked_by, "reason": None, "is_error": is_error,
        "result_preview": result_preview, "result_truncated": result_truncated, "identity": None, "latency_ms": 3,
        "scripted": i is not None, "request_id": f"req-{i}",
    }


def trace(*steps):
    rows = list(steps)
    for n, row in enumerate(rows, start=1):
        row["n"] = n
    return rows


def failed(result: ExpectResult) -> list[Check]:
    return [c for c in result.checks if not c.ok]


def assert_ok(result: ExpectResult):
    assert not result.skipped
    assert result.ok, [(c.description, c.detail) for c in failed(result)]
    assert result.checks


def assert_fails(result: ExpectResult):
    assert not result.skipped
    assert result.ok is False
    assert failed(result)


# --- select_block ----------------------------------------------------------------------------------


def test_select_block_picks_the_env():
    expect = {"tenant": {"steps": [{"i": 0, "status": 200}]}, "local": {}}
    assert select_block(expect, "tenant") == {"steps": [{"i": 0, "status": 200}]}
    assert select_block(expect, "local") == {}
    assert select_block(expect, "staging") is None
    assert select_block(None, "tenant") is None


# --- empty blocks ----------------------------------------------------------------------------------


@pytest.mark.parametrize("block", [None, {}])
def test_empty_or_missing_block_is_skipped_and_not_a_pass(block):
    result = evaluate(block, trace(step(0)))
    assert result.skipped is True
    assert result.ok is False


# --- step selection --------------------------------------------------------------------------------


def test_i_selects_by_the_step_i_field_not_the_trace_position():
    rows = trace(
        step(None, "token", 200),                 # the agent's own exchange: unscripted, i=None
        step(0, "tool", 200),
        step(1, "tool", 403, "xc"),
    )
    assert_ok(evaluate({"steps": [{"i": 1, "status": 403, "blocked_by": "xc"}]}, rows))
    assert_fails(evaluate({"steps": [{"i": 0, "status": 403}]}, rows))


def test_i_that_is_not_in_the_trace_fails():
    assert_fails(evaluate({"steps": [{"i": 5, "status": 200}]}, trace(step(0), step(1))))


def test_only_keys_present_in_the_spec_are_compared():
    rows = trace(step(0, "tool", 403, "xc", result_preview="Forbidden"))
    assert_ok(evaluate({"steps": [{"i": 0, "status": 403}]}, rows))
    assert_ok(evaluate({"steps": [{"i": 0, "blocked_by": "xc"}]}, rows))
    assert_fails(evaluate({"steps": [{"i": 0, "blocked_by": "server"}]}, rows))


def test_blocked_by_null_asserts_not_blocked():
    spec = {"steps": [{"i": 0, "status": 403, "blocked_by": None}]}
    assert_ok(evaluate(spec, trace(step(0, "http", 403, None))))
    assert_fails(evaluate(spec, trace(step(0, "http", 403, "xc"))))


def test_status_null_means_no_http_response():
    spec = {"steps": [{"i": 0, "status": None, "blocked_by": "network"}]}
    assert_ok(evaluate(spec, trace(step(0, "http", None, "network", result_preview=None))))
    assert_fails(evaluate(spec, trace(step(0, "http", 502, "network"))))


def test_is_error_key_is_compared_when_present():
    rows = trace(step(0, "tool", 200, None, is_error=True, result_preview="not your order"))
    assert_ok(evaluate({"steps": [{"i": 0, "is_error": True}]}, rows))
    assert_fails(evaluate({"steps": [{"i": 0, "is_error": False}]}, rows))


# --- status 200 on a tool step ---------------------------------------------------------------------


def test_tool_status_200_with_is_error_true_fails():
    rows = trace(step(0, "tool", 200, None, is_error=True, result_preview="not your order"))
    assert_fails(evaluate({"steps": [{"i": 0, "status": 200}]}, rows))


def test_status_200_on_a_non_tool_step_ignores_is_error():
    assert_ok(evaluate({"steps": [{"i": 0, "status": 200}]}, trace(step(0, "model", 200, None, is_error=None))))
    assert_ok(evaluate({"steps": [{"i": 0, "status": 200}]}, trace(step(0, "http", 200, None, is_error=True))))


def test_tool_is_error_steps_do_not_count_as_status_200_in_a_range():
    rows = trace(step(0, "tool", 200), step(1, "tool", 200, is_error=True), step(2, "tool", 200))
    assert_ok(evaluate({"steps": [{"i_range": [0, 2], "status": 200, "count": 2}]}, rows))
    assert_fails(evaluate({"steps": [{"i_range": [0, 2], "status": 200, "count": 3}]}, rows))


# --- ranges ----------------------------------------------------------------------------------------


def test_i_range_without_count_requires_every_index_to_be_present_and_match():
    spec = {"steps": [{"i_range": [1, 3], "status": 200}]}
    assert_ok(evaluate(spec, trace(step(0, "model"), step(1), step(2), step(3), step(4, status=429))))
    assert_fails(evaluate(spec, trace(step(1), step(2, status=429, blocked_by="xc"), step(3))))
    assert_fails(evaluate(spec, trace(step(1), step(3))))      # i=2 missing


def test_i_range_is_inclusive_on_both_ends():
    spec = {"steps": [{"i_range": [1, 3], "status": 200}]}
    assert_fails(evaluate(spec, trace(step(1), step(2), step(3, status=429))))
    assert_fails(evaluate(spec, trace(step(1, status=429), step(2), step(3))))


def s4_like_trace(successes, total=40):
    rows = [step(0, "model", 200)]
    for i in range(1, total + 1):
        rows.append(step(i, "tool", 200) if i <= successes else step(i, "tool", 429, "xc", result_preview="Too Many"))
    return trace(*rows)


@pytest.mark.parametrize("successes, ok", [(8, False), (9, True), (10, True), (11, True), (12, False)])
def test_count_with_tolerance(successes, ok):
    spec = {"steps": [{"i_range": [1, 40], "status": 200, "count": 10, "tolerance": 1}]}
    assert evaluate(spec, s4_like_trace(successes)).ok is ok


@pytest.mark.parametrize("successes, ok", [(9, False), (10, True), (11, False)])
def test_count_without_tolerance_is_exact(successes, ok):
    spec = {"steps": [{"i_range": [1, 40], "status": 200, "count": 10}]}
    assert evaluate(spec, s4_like_trace(successes)).ok is ok


@pytest.mark.parametrize("successes, ok", [(10, True), (11, True), (12, False)])
def test_min_count(successes, ok):
    spec = {"steps": [{"i_range": [1, 40], "status": 429, "blocked_by": "xc", "min_count": 29}]}
    assert evaluate(spec, s4_like_trace(successes)).ok is ok


def test_count_only_looks_inside_the_range():
    rows = trace(step(0, "tool", 200), step(1, "tool", 200), step(2, "tool", 429, "xc"), step(3, "tool", 200))
    assert_ok(evaluate({"steps": [{"i_range": [1, 2], "status": 200, "count": 1}]}, rows))


# --- body_not_contains -----------------------------------------------------------------------------


def test_body_not_contains_string_passes_when_absent_and_fails_when_present():
    spec = {"steps": [{"i": 0, "status": 200, "body_not_contains": CARD}]}
    assert_ok(evaluate(spec, trace(step(0, result_preview='{"card": "XXXXXXXXXXXX1111"}'))))
    assert_fails(evaluate(spec, trace(step(0, result_preview=f'{{"card": "{CARD}"}}'))))


def test_body_not_contains_list_fails_on_any_item():
    spec = {"steps": [{"i": 0, "body_not_contains": [CARD, CARD_SPACED]}]}
    assert_ok(evaluate(spec, trace(step(0, result_preview="masked"))))
    assert_fails(evaluate(spec, trace(step(0, result_preview=f"card {CARD_SPACED}"))))


def test_body_not_contains_alt_is_also_checked():
    spec = {"steps": [{"i": 0, "body_not_contains": CARD, "body_not_contains_alt": CARD_SPACED}]}
    assert_ok(evaluate(spec, trace(step(0, result_preview="masked"))))
    assert_fails(evaluate(spec, trace(step(0, result_preview=f"card {CARD_SPACED}"))))


def test_body_not_contains_fails_when_the_preview_was_truncated():
    spec = {"steps": [{"i": 0, "status": 200, "body_not_contains": CARD}]}
    rows = trace(step(0, result_preview="x" * 4000, result_truncated=True))
    assert_fails(evaluate(spec, rows))


# --- estate ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("observed, ok", [(9, True), (10, True), (11, True), (12, False), (8, False)])
def test_estate_delta_with_tolerance(observed, ok):
    block = {"estate_delta": {"stock_writes": 10}, "tolerance": {"stock_writes": 1}}
    assert evaluate(block, [], estate_delta={"stock_writes": observed, "refunds_issued": 0}).ok is ok


@pytest.mark.parametrize("observed, ok", [(0, True), (1, False)])
def test_estate_delta_default_tolerance_is_zero(observed, ok):
    block = {"estate_delta": {"refunds_issued": 0}, "tolerance": {"stock_writes": 1}}
    assert evaluate(block, [], estate_delta={"refunds_issued": observed, "stock_writes": 5}).ok is ok


def test_missing_estate_observation_fails():
    result = evaluate({"steps": [{"i": 0, "status": 200}], "estate_delta": {"refunds_issued": 0}}, trace(step(0)))
    assert_fails(result)
    assert any("estate not observed" in f"{c.description} {c.detail}".lower() for c in failed(result))


# --- front door, soft, hints -----------------------------------------------------------------------


def front_door(attempt, status, blocked_by):
    return {"kind": "front_door", "attempt": attempt, "status": status, "blocked_by": blocked_by}


def test_front_door_items_match_by_attempt():
    block = {"front_door": [{"attempt": 2, "status": 403, "blocked_by": "xc"}]}
    assert_ok(evaluate(block, [front_door(1, 200, None), front_door(2, 403, "xc")]))
    assert_fails(evaluate(block, [front_door(1, 403, "xc"), front_door(2, 200, None)]))
    assert_fails(evaluate(block, [front_door(1, 403, "xc")]))


def test_front_door_ignores_non_front_door_rows():
    block = {"front_door": [{"attempt": 1, "status": 403, "blocked_by": "xc"}]}
    rows = [{"kind": "tool", "attempt": 1, "i": 0, "status": 403, "blocked_by": "xc"}]
    assert_fails(evaluate(block, rows))


def test_soft_items_never_fail_and_are_labelled():
    block = {"steps": [{"i": 0, "status": 200}], "soft": ["threat level rises", "eventual block"]}
    result = evaluate(block, trace(step(0)))
    assert_ok(result)
    soft = [c for c in result.checks if c.description.startswith("(soft) ")]
    assert len(soft) == 2
    assert all(c.ok for c in soft)


def test_soft_does_not_rescue_a_failing_block():
    block = {"steps": [{"i": 0, "status": 403}], "soft": ["anything"]}
    assert_fails(evaluate(block, trace(step(0))))


def test_xc_log_hint_is_not_asserted():
    block = {"steps": [{"i": 0, "status": 200}], "xc_log_hint": "for humans"}
    assert_ok(evaluate(block, trace(step(0))))


# --- every REAL scenario's expect blocks are satisfiable -------------------------------------------


def exchanges():
    """Server-mode agents trace their own token exchanges with i=None before the plan."""
    return [step(None, "token", 200), step(None, "token", 200)]


def closing_model_call():
    """The unscripted closing reply (final_reply null and the plan does not end on a model step)."""
    return [step(None, "model", 200, result_preview="Done.")]


def masked_order():
    return '{"order_id": "CO-1042", "card": "XXXXXXXXXXXX1111", "email": "s***@example.com"}'


PASSING = {
    "S0": (trace(*exchanges(), *[
        step(0, "tool", 200, tool="catalog.get_reviews"),
        step(1, "tool", 200, tool="orders.get_order_status"),
        step(2, "model", 200, result_preview="Your order is on its way."),
    ]), {"refunds_issued": 0, "stock_writes": 0}),
    "S1": (trace(*exchanges(), *[
        step(0, "tool", 200, tool="catalog.get_reviews"),
        step(1, "model", 200),
        step(2, "tool", 403, "xc", tool="refunds.issue_refund", result_preview="Forbidden"),
    ]), {"refunds_issued": 0, "stock_writes": 0}),
    "S1b": (trace(*exchanges(), *[
        step(0, "tool", 400, "server", tool="orders.get_order_details",
             result_preview='{"jsonrpc":"2.0","id":"1","error":{"code":-32020}}'),
    ]), {"refunds_issued": 0, "stock_writes": 0}),
    "S2": (trace(*exchanges(), *[
        step(0, "tool", 200, tool="orders.get_order_details", result_preview=masked_order()),
        step(1, "model", 200),
    ]), {"refunds_issued": 0, "stock_writes": 0}),
    "S3a": (trace(*exchanges(), *[
        step(0, "tool", 200, tool="orders.get_order_details"),
        step(1, "tool", 403, "xc", tool="refunds.issue_refund", result_preview="Forbidden"),
    ], *closing_model_call()), {"refunds_issued": 0, "stock_writes": 0}),
    "S3b": (trace(*exchanges(), *[
        step(0, "tool", 200, tool="orders.get_order_details"),
        step(1, "tool", 200, tool="refunds.issue_refund"),
    ], *closing_model_call()), {"refunds_issued": 1, "stock_writes": 0}),
    "S4": (trace(*exchanges(), *s4_like_trace(10)), {"refunds_issued": 0, "stock_writes": 10}),
    "S5": (trace(*[
        step(0, "token", 200),
        step(1, "token", 200),
        step(2, "tool", 403, "xc", tool="orders.list_customer_orders", result_preview="Forbidden"),
        step(3, "model", 403, "xc", result_preview="Forbidden"),
        step(4, "http", None, "network", result_preview=None),
        step(5, "http", 403, "xc", result_preview="Forbidden"),
    ]), {"refunds_issued": 0, "stock_writes": 0}),
    "S6": (trace(*[
        step(0, "token", 200),
        step(1, "http", 200),
        step(2, "http", 403, "xc"),
        step(3, "http", 403, "xc"),
    ], *[step(i, "http", 200) if i < 24 else step(i, "http", 429, "xc") for i in range(4, 54)]),
        {"refunds_issued": 0, "stock_writes": 0}),
    "S7": ([front_door(k, 403, "xc") for k in range(1, 9)], {"refunds_issued": 0, "stock_writes": 0}),
}


def real_blocks():
    cases = []
    for sc in load_scenarios(SCENARIOS_DIR):
        for env in ("local", "tenant"):
            cases.append(pytest.param(sc, env, id=f"{sc.id}-{env}"))
    return cases


def test_every_real_scenario_has_a_synthetic_trace():
    assert sorted(s.id for s in load_scenarios(SCENARIOS_DIR)) == sorted(PASSING)


@pytest.mark.parametrize("scenario, env", real_blocks())
def test_real_expect_block_is_satisfiable(scenario, env):
    block = select_block(scenario.expect, env)
    if not block:
        pytest.skip(f"{scenario.id} has no {env} expectations")
    rows, estate = PASSING[scenario.id]
    assert_ok(evaluate(block, rows, estate_delta=estate))


def test_s7_local_block_is_empty_and_reports_skipped():
    s7 = next(s for s in load_scenarios(SCENARIOS_DIR) if s.id == "S7")
    result = evaluate(select_block(s7.expect, "local"), PASSING["S7"][0])
    assert result.skipped is True and result.ok is False


def test_real_s2_tenant_block_rejects_an_unmasked_card():
    s2 = next(s for s in load_scenarios(SCENARIOS_DIR) if s.id == "S2")
    rows = trace(step(0, "tool", 200, result_preview=f'{{"card": "{CARD_SPACED}"}}'), step(1, "model", 200))
    assert_fails(evaluate(select_block(s2.expect, "tenant"), rows))
