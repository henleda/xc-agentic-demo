import asyncio

import httpx
import pytest
from copperline_common.expect import evaluate, select_block
from copperline_common.plan_executor import ExecContext, run_plan
from copperline_common.scenarios import by_id, load_scenarios


def step(i, status, kind="tool", blocked_by=None, is_error=..., preview=None, truncated=False) -> dict:
    """A trace row. By default a 200 tool row carries is_error=False, as a parsed MCP result would."""
    if is_error is ...:
        is_error = False if (kind == "tool" and status == 200) else None
    return {"n": i + 1, "i": i, "kind": kind, "status": status, "blocked_by": blocked_by, "is_error": is_error,
            "result_preview": preview, "result_truncated": truncated}


def test_select_block():
    assert select_block({"tenant": {"steps": []}, "local": {}}, "tenant") == {"steps": []}
    assert select_block(None, "tenant") is None and select_block({}, "local") is None


def test_single_steps_and_blocked_by_null():
    trace = [step(0, 200), step(1, 403, blocked_by="xc"), {"n": 3, "i": None, "kind": "model", "status": 200}]
    block = {"steps": [{"i": 0, "status": 200, "blocked_by": None}, {"i": 1, "status": 403, "blocked_by": "xc"}]}
    assert evaluate(block, trace).ok
    bad = {"steps": [{"i": 1, "status": 403, "blocked_by": None}]}
    result = evaluate(bad, trace)
    assert not result.ok and "blocked_by" in result.checks[0].detail
    missing = evaluate({"steps": [{"i": 7, "status": 200}]}, trace)
    assert not missing.ok and missing.checks[0].detail == "step not in trace"


def test_status_200_tool_requires_no_is_error():
    assert not evaluate({"steps": [{"i": 0, "status": 200}]}, [step(0, 200, is_error=True)]).ok
    assert evaluate({"steps": [{"i": 0, "status": 200}]}, [step(0, 200, kind="http", is_error=True)]).ok


def test_status_200_tool_without_an_mcp_result_fails():
    """HTTP 200 with no parsed MCP result (HTML block page, empty body, JSON-RPC error) is not a tool success."""
    no_result = step(0, 200, is_error=None, preview="<html>Request Rejected. Your support ID is 123</html>")
    result = evaluate({"steps": [{"i": 0, "status": 200, "blocked_by": None}]}, [no_result])
    assert not result.ok and result.checks[0].detail == "status 200 but no MCP result"
    assert not evaluate({"steps": [{"i_range": [0, 0], "status": 200, "min_count": 1}]}, [no_result]).ok
    assert evaluate({"steps": [{"i": 0, "status": 200}]}, [step(0, 200, is_error=False)]).ok
    assert evaluate({"steps": [{"i": 0, "status": 200}]}, [step(0, 200, kind="model", is_error=None)]).ok


def test_status_null_means_no_response():
    trace = [step(0, None, kind="http", blocked_by="network")]
    assert evaluate({"steps": [{"i": 0, "status": None, "blocked_by": "network"}]}, trace).ok


def test_i_is_the_plan_index_not_the_trace_position():
    trace = [{"n": 1, "i": None, "kind": "token", "status": 200}, step(0, 403, blocked_by="xc")]
    assert evaluate({"steps": [{"i": 0, "status": 403}]}, trace).ok


def test_range_count_with_tolerance_and_min_count():
    trace = [step(0, 200, kind="model")]
    trace += [step(i, 200) for i in range(1, 12)]  # 11 successes
    trace += [step(i, 429, blocked_by="xc") for i in range(12, 41)]  # 29 fabric 429s
    block = {"steps": [
        {"i_range": [1, 40], "status": 200, "count": 10, "tolerance": 1},
        {"i_range": [1, 40], "status": 429, "blocked_by": "xc", "min_count": 29},
    ]}
    assert evaluate(block, trace).ok
    too_strict = {"steps": [{"i_range": [1, 40], "status": 200, "count": 10}]}
    assert not evaluate(too_strict, trace).ok


def test_range_without_count_needs_every_index():
    trace = [step(i, 200) for i in range(1, 11)]
    assert evaluate({"steps": [{"i_range": [1, 10], "status": 200}]}, trace).ok
    assert not evaluate({"steps": [{"i_range": [1, 11], "status": 200}]}, trace).ok
    trace[3]["status"] = 429
    assert not evaluate({"steps": [{"i_range": [1, 10], "status": 200}]}, trace).ok


@pytest.mark.parametrize(
    "preview, truncated, ok",
    [
        ('{"card": "**** 1111"}', False, True),
        ('{"card": "4111111111111111"}', False, False),
        ('{"card": "4111 1111 1111 1111"}', False, False),
        ('{"card": "**** 1111"}', True, False),  # truncated previews can never prove absence
    ],
)
def test_body_not_contains(preview, truncated, ok):
    spec = {"i": 0, "status": 200, "blocked_by": None, "body_not_contains": "4111111111111111",
            "body_not_contains_alt": "4111 1111 1111 1111"}
    assert evaluate({"steps": [spec]}, [step(0, 200, preview=preview, truncated=truncated)]).ok is ok
    as_list = {"i": 0, "body_not_contains": ["4111111111111111", "4111 1111 1111 1111"]}
    assert evaluate({"steps": [as_list]}, [step(0, 200, preview=preview, truncated=truncated)]).ok is ok


def test_estate_delta():
    block = {"steps": [{"i": 0, "status": 200}], "estate_delta": {"stock_writes": 10, "refunds_issued": 0},
             "tolerance": {"stock_writes": 1}}
    trace = [step(0, 200)]
    assert evaluate(block, trace, estate_delta={"stock_writes": 11, "refunds_issued": 0}).ok
    assert not evaluate(block, trace, estate_delta={"stock_writes": 12, "refunds_issued": 0}).ok
    assert not evaluate(block, trace, estate_delta={"stock_writes": 10, "refunds_issued": 1}).ok
    unobserved = evaluate(block, trace)
    assert not unobserved.ok and any(c.detail == "estate not observed" for c in unobserved.checks)


def test_front_door_and_soft():
    block = {"front_door": [{"attempt": 1, "status": 403, "blocked_by": "xc"}], "soft": ["threat level rises"],
             "xc_log_hint": "ignored"}
    trace = [{"kind": "front_door", "attempt": 1, "status": 403, "blocked_by": "xc"},
             {"kind": "front_door", "attempt": 2, "status": 200, "blocked_by": None}]
    result = evaluate(block, trace)
    assert result.ok and not result.skipped
    soft = [c for c in result.checks if c.description.startswith("(soft) ")]
    assert soft and soft[0].ok
    assert not evaluate({"front_door": [{"attempt": 3, "status": 403}]}, trace).ok


@pytest.mark.parametrize("block", [None, {}, {"xc_log_hint": "x"}, {"soft": ["only soft"]}])
def test_empty_block_is_skipped_not_passed(block):
    result = evaluate(block, [step(0, 200)])
    assert result.skipped is True and result.ok is False


@pytest.mark.parametrize(
    "mcp_body",
    [
        "<html>Request Rejected. The requested URL was rejected. Your support ID is: 123</html>",
        "",
        '{"jsonrpc": "2.0", "id": "1", "error": {"code": -32603, "message": "boom"}}',
    ],
    ids=["html-block-page", "empty-body", "jsonrpc-error"],
)
@pytest.mark.parametrize("scenario_id, estate", [("S0", {"refunds_issued": 0}), ("S2", None)])
def test_real_scenarios_fail_when_tools_return_200_without_an_mcp_result(repo_root, mcp_body, scenario_id, estate):
    """End to end through run_plan: a 200 from /mcp/* that is not an MCP result must not pass S0 or S2."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/mcp/"):
            return httpx.Response(200, headers={"x-copperline-origin": "mcp-servers"}, text=mcp_body)
        body = {"output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}}, "stop_reason": "end_turn"}
        return httpx.Response(200, headers={"x-copperline-origin": "ai-gw"}, json=body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ctx = ExecContext(http=http, tools_base_url="http://tools", llm_base_url="http://llm", idp_base_url=None,
                              tokens={"aud:copperline-tools": "t", "aud:copperline-llm": "l"}, env={})
            return await run_plan(sc, ctx)

    sc = by_id(load_scenarios(repo_root / "scenarios"), scenario_id)
    outcome = asyncio.run(go())
    rows = [s.to_dict() for s in outcome.steps]
    assert any(row["kind"] == "tool" and row["status"] == 200 and row["is_error"] is None for row in rows)
    result = evaluate(select_block(sc.expect, "tenant"), rows, estate_delta=estate)
    assert not result.ok
    assert any(c.detail == "status 200 but no MCP result" for c in result.checks if not c.ok)
