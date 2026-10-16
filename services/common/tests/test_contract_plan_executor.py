"""Black-box contract tests for plan_executor.run_plan against a fake world (httpx.MockTransport).

Source of truth: API.md (plan_executor.py), scenarios/README.md ("How a plan runs") and the trace Step
in docs/02-agents.md. The fake world has an IdP, MCP tool servers, an AI gateway and HTTP targets. Responses
from "our" services carry x-copperline-origin; fabric blocks (403/429 at an LB) do not.
"""
import asyncio
import json
import time
import urllib.parse
import uuid
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
from copperline_common.outbound import new_http_client
from copperline_common.plan_executor import PREVIEW_LIMIT, ExecContext, PlanRun, Step, run_plan
from copperline_common.scenarios import PlanError, Scenario, by_id, load_scenarios
from cryptography.hazmat.primitives.asymmetric import rsa

REPO = Path(__file__).resolve().parents[3]
SCENARIOS = load_scenarios(REPO / "scenarios")

TOOLS = "http://tools.test"
LLM = "http://llm.test"
IDP = "http://idp.test"
PARTNER = "http://partner.test"
LBS = (TOOLS, LLM, IDP, PARTNER)
STOREOPS_SECRET = "storeops-SECRET-9f8e7d"
PARTNER_SECRET = "granite-SECRET-1a2b3c"
ENV = {
    "TOOLS_BASE_URL": TOOLS,
    "IDP_BASE_URL": IDP,
    "PARTNER_BASE_URL": PARTNER,
    "EXFIL_TEST_URL": "http://exfil.test/drop",
    "AGENT_STOREOPS_SECRET": STOREOPS_SECRET,
    "GRANITE_SUPPLY_SECRET": PARTNER_SECRET,
}
TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CLIENT_CLAIMS = {   # what the fake IdP mints for client_credentials (machine mode: no human behind it)
    "agent-storeops": {"sub": "storeops", "agent_id": "storeops", "agent_role": "storeops",
                       "obo_sub": "none", "obo_role": "none"},
    "granite-supply": {"sub": "granite-supply", "role": "partner"},
}
DENIED_TOOLS = {"issue_refund", "list_customer_orders"}   # the fake fabric denies these at tools-lb
STEP_FIELDS = {"n", "i", "kind", "target", "tool", "mcp_name_header", "args_preview", "status", "blocked_by", "reason",
               "is_error", "result_preview", "result_truncated", "identity", "latency_ms", "scripted", "request_id"}
REQUIRED_FIELDS = {"n", "i", "kind", "target", "status", "blocked_by", "result_truncated", "latency_ms", "scripted",
                   "request_id"}   # docs/02 marks the others with "?"


def origin(service):
    return {"x-copperline-origin": service}


def fabric_block(status=403, text="Forbidden"):
    """A response the XC LB generated: no origin header."""
    return httpx.Response(status, text=text, headers={"content-type": "text/plain"})


def tool_response(body, text, *, is_error=False):
    result = {"content": [{"type": "text", "text": text}], "isError": is_error, "resultType": "complete"}
    if not is_error:
        result["structuredContent"] = json.loads(text) if text.startswith("{") else {"result": text}
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}, headers=origin("mcp"))


def converse_response(text, route):
    payload = {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stop_reason": "end_turn",
               "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}, "route": route, "model_id": "m"}
    return httpx.Response(200, json=payload, headers=origin("ai-gw"))


class World:
    """The fake network. Records every request; behaviour is tuned per test through attributes."""

    def __init__(self, *, adjust_allowed=10, partner_pages_allowed=20):
        self.requests: list[httpx.Request] = []
        self.token_forms: list[dict] = []
        self.minted: dict[tuple[str, str], str] = {}
        self.tool_text: dict[str, str] = {}
        self.adjust_allowed = adjust_allowed
        self.adjust_calls = 0
        self.partner_pages_allowed = partner_pages_allowed
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if path.endswith("/auth/token"):
            return self.idp(request)
        if host == "tools.test":
            return self.tools(request)
        if host == "llm.test":
            return self.llm(request)
        if host == "exfil.test":
            raise httpx.ConnectError("connection refused", request=request)
        if host == "partner.test":
            return await self.partner(request)
        return fabric_block(403)

    def to(self, host):
        return [r for r in self.requests if r.url.host == host and not r.url.path.endswith("/auth/token")]

    def idp(self, request):
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.token_forms.append(form)
        now = int(time.time())
        claims = {**CLIENT_CLAIMS[form["client_id"]], "aud": form["audience"], "iss": "test-idp", "iat": now,
                  "exp": now + 300, "jti": uuid.uuid4().hex}
        token = pyjwt.encode(claims, TEST_KEY, algorithm="RS256", headers={"kid": "test-kid"})
        self.minted[(form["client_id"], form["audience"])] = token
        return httpx.Response(200, json={"access_token": token, "token_type": "Bearer", "expires_in": 300},
                              headers=origin("idp"))

    def tools(self, request):
        if request.method != "POST":
            return fabric_block(403)                       # API validation at tools-lb
        body = json.loads(request.content)
        name, args = body["params"]["name"], body["params"].get("arguments", {})
        if request.headers.get("mcp-name") != name:
            error = {"code": -32020, "message": "HeaderMismatch", "data": {"header": "Mcp-Name"}}
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": body["id"], "error": error}, headers=origin("mcp"))
        if name in DENIED_TOOLS:
            return fabric_block(403)
        if name == "adjust_stock":
            self.adjust_calls += 1
            if self.adjust_calls > self.adjust_allowed:
                return fabric_block(429, "Too Many Requests")
        if args.get("order_id") == "CO-9999":
            return tool_response(body, "not your order", is_error=True)
        return tool_response(body, self.tool_text.get(name) or json.dumps({"tool": name, "args": args}))

    def llm(self, request):
        route = request.headers.get("x-model-route")
        if route == "premium":
            return fabric_block(403)
        return converse_response(f"reply from {route}", route)

    async def partner(self, request):
        path = request.url.path
        if path in ("/v1/internal/cost-sheet", "/v1/asn"):
            return fabric_block(403)
        page = request.url.params.get("page")
        if page is None:
            return httpx.Response(200, json={"purchase_orders": []}, headers=origin("partner-api"))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep((10 - (int(page) - 1) % 10) * 0.005)   # later pages in a batch finish first
        self.in_flight -= 1
        if int(page) > self.partner_pages_allowed:
            return fabric_block(429, "Too Many Requests")
        return httpx.Response(200, json={"page": int(page)}, headers=origin("partner-api"))


@pytest.fixture(autouse=True)
def secrets_in_env(monkeypatch):
    monkeypatch.setenv("AGENT_STOREOPS_SECRET", STOREOPS_SECRET)
    monkeypatch.setenv("GRANITE_SUPPLY_SECRET", PARTNER_SECRET)


def scenario(plan, *, sid="T1", final_reply=None, agent="shopper", persona="alex.rivera"):
    return Scenario(id=sid, title="test", persona=persona, agent=agent, message="test message", trigger="test",
                    plan=plan, final_reply=final_reply, expect={}, repeat_message=None, source="test.yaml")


def run(world, sc, *, start_n=1, **ctx_overrides) -> PlanRun:
    async def go():
        async with new_http_client(transport=httpx.MockTransport(world)) as http:
            kwargs = {
                "http": http, "tools_base_url": TOOLS, "llm_base_url": LLM, "idp_base_url": IDP,
                "tokens": {"aud:copperline-tools": "server-tools-tok", "aud:copperline-llm": "server-llm-tok"},
                "agent_id": "shopper", "obo_sub": "alex.rivera", "lb_base_urls": LBS,
                "conversation": [{"role": "user", "content": "Hi, where is my order CO-1042?"}],
                "system_prompt": "You are the Copperline shopper agent.", "scenario_id": sc.id, "env": ENV,
            }
            kwargs.update(ctx_overrides)
            return await run_plan(sc, ExecContext(**kwargs), start_n=start_n)

    return asyncio.run(go())


def run_batch(world, sc):
    """Batch mode (insights-nightly / partner): no pre-exchanged tokens, no human, no conversation."""
    return run(world, sc, tokens={}, agent_id=None, obo_sub=None, conversation=[], system_prompt=None)


def converse_text(body):
    return "\n".join(block["text"] for m in body["messages"] for block in m["content"] if "text" in block)


S0_LIKE = [
    {"tool": "catalog.get_reviews", "args": {"product_id": "TNT-ALPINE-2"}},
    {"tool": "orders.get_order_status", "args": {"order_id": "CO-1042"}},
    {"model": {"route": "fast"}},
]


# --- indexing ---------------------------------------------------------------------------------------


def test_steps_carry_plan_index_and_sequential_n_from_start_n():
    result = run(World(), scenario(S0_LIKE), start_n=3)
    assert [s.i for s in result.steps] == [0, 1, 2]
    assert [s.n for s in result.steps] == [3, 4, 5]
    assert [s.kind for s in result.steps] == ["tool", "tool", "model"]
    assert all(s.scripted for s in result.steps)
    assert result.stopped_early is False


def test_step_to_dict_has_the_trace_fields():
    result = run(World(), scenario(S0_LIKE))
    row = result.steps[0].to_dict()
    assert REQUIRED_FIELDS <= set(row) <= STEP_FIELDS
    assert row["tool"] == "catalog.get_reviews" and row["i"] == 0 and row["n"] == 1
    json.dumps([s.to_dict() for s in result.steps])          # JSON-serialisable


# --- token steps ------------------------------------------------------------------------------------


def test_token_steps_record_machine_identity_with_obo_role_none():
    world = World()
    result = run_batch(world, by_id(SCENARIOS, "S5"))
    tok_tools, tok_llm = result.steps[0], result.steps[1]
    assert (tok_tools.i, tok_tools.kind, tok_tools.status, tok_tools.blocked_by) == (0, "token", 200, None)
    assert tok_tools.target == f"{IDP}/auth/token"
    assert tok_tools.identity == {"agent_id": "storeops", "agent_role": "storeops", "obo_sub": "none",
                                  "obo_role": "none", "aud": "copperline-tools", "sub": "storeops"}
    assert tok_llm.identity["aud"] == "copperline-llm"
    assert tok_llm.identity["obo_role"] == "none"


def test_token_request_is_a_client_credentials_form_using_the_env_secret():
    world = World()
    run_batch(world, by_id(SCENARIOS, "S5"))
    assert world.token_forms[0] == {"grant_type": "client_credentials", "client_id": "agent-storeops",
                                    "client_secret": STOREOPS_SECRET, "audience": "copperline-tools"}
    assert world.token_forms[1]["audience"] == "copperline-llm"


def test_token_identity_omits_claims_the_token_does_not_have():
    result = run_batch(World(), by_id(SCENARIOS, "S6"))
    token = result.steps[0]
    assert token.target == f"{PARTNER}/auth/token"          # spec.url wins over the IdP default
    assert token.identity == {"sub": "granite-supply", "aud": "copperline-partner"}


def test_secrets_never_appear_in_any_step_field():
    world = World()
    steps = run_batch(world, by_id(SCENARIOS, "S5")).steps + run_batch(world, by_id(SCENARIOS, "S6")).steps
    assert {f["client_secret"] for f in world.token_forms} == {STOREOPS_SECRET, PARTNER_SECRET}   # really sent
    dumped = json.dumps([s.to_dict() for s in steps])
    assert STOREOPS_SECRET not in dumped
    assert PARTNER_SECRET not in dumped


def test_token_steps_replace_the_audience_tokens_for_later_steps():
    world = World()
    run_batch(world, by_id(SCENARIOS, "S5"))
    tools_tok = world.minted[("agent-storeops", "copperline-tools")]
    llm_tok = world.minted[("agent-storeops", "copperline-llm")]
    mcp_call, export = world.to("tools.test")
    (model_call,) = world.to("llm.test")
    assert mcp_call.headers["authorization"] == f"Bearer {tools_tok}"
    assert model_call.headers["authorization"] == f"Bearer {llm_tok}"
    assert export.headers["authorization"] == f"Bearer {tools_tok}"          # auth: tools_tok (save_as)


def test_real_s5_plan_produces_the_expected_outcomes():
    result = run_batch(World(), by_id(SCENARIOS, "S5"))
    outcome = [(s.i, s.kind, s.status, s.blocked_by) for s in result.steps]
    assert outcome == [
        (0, "token", 200, None),
        (1, "token", 200, None),
        (2, "tool", 403, "xc"),
        (3, "model", 403, "xc"),
        (4, "http", None, "network"),
        (5, "http", 403, "xc"),
    ]
    assert result.reply == "(batch job; no reply)"


# --- tool steps -------------------------------------------------------------------------------------


def test_tool_steps_use_the_tools_audience_token_and_model_steps_the_llm_one():
    world = World()
    run(world, scenario(S0_LIKE))
    assert [r.headers["authorization"] for r in world.to("tools.test")] == ["Bearer server-tools-tok"] * 2
    assert [r.headers["authorization"] for r in world.to("llm.test")] == ["Bearer server-llm-tok"]


def test_tool_step_fields():
    world = World()
    world.tool_text["get_reviews"] = '{"reviews": ["Great tent, stays dry."]}'
    step = run(world, scenario(S0_LIKE)).steps[0]
    assert step.kind == "tool"
    assert step.target == f"{TOOLS}/mcp/catalog"
    assert step.tool == "catalog.get_reviews"
    assert step.mcp_name_header is None
    assert json.loads(step.args_preview) == {"product_id": "TNT-ALPINE-2"}
    assert (step.status, step.blocked_by, step.is_error) == (200, None, False)
    assert step.result_preview == '{"reviews": ["Great tent, stays dry."]}'
    assert step.result_truncated is False
    assert step.identity is None
    request = world.to("tools.test")[0]
    assert request.headers["mcp-method"] == "tools/call" and request.headers["mcp-name"] == "get_reviews"


def test_tool_error_result_sets_is_error_and_previews_the_message():
    step = run(World(), scenario([{"tool": "orders.get_order_status", "args": {"order_id": "CO-9999"}}])).steps[0]
    assert (step.status, step.blocked_by, step.is_error) == (200, None, True)
    assert step.result_preview == "not your order"


def test_fabric_block_on_a_tool_step_is_xc_even_without_lb_base_urls():
    sc = scenario([{"tool": "refunds.issue_refund", "args": {"order_id": "CO-1077", "amount": 499}}])
    step = run(World(), sc, lb_base_urls=()).steps[0]
    assert (step.status, step.blocked_by) == (403, "xc")
    assert step.is_error is None
    assert step.result_preview == "Forbidden"                 # raw body when there is no tool text


def test_header_name_override_is_sent_and_recorded_on_the_step():
    world = World()
    result = run(world, by_id(SCENARIOS, "S1b"))
    (step,) = result.steps
    assert step.tool == "orders.get_order_details"
    assert step.mcp_name_header == "get_order_status"
    assert (step.status, step.blocked_by) == (400, "server")
    (request,) = world.to("tools.test")
    assert request.headers["mcp-name"] == "get_order_status"
    assert json.loads(request.content)["params"]["name"] == "get_order_details"
    assert result.reply == "Sorry, I can only share the status of that order."


def test_override_equal_to_the_tool_name_does_not_set_mcp_name_header():
    plan = [{"tool": "orders.get_order_status", "args": {"order_id": "CO-1042"},
             "header_name_override": "get_order_status"}]
    step = run(World(), scenario(plan)).steps[0]
    assert step.mcp_name_header is None
    assert step.status == 200


# --- model steps ------------------------------------------------------------------------------------


def test_model_step_sends_route_and_scenario_headers_and_no_tools():
    world = World()
    step = run(world, scenario(S0_LIKE, sid="S0")).steps[2]
    (request,) = world.to("llm.test")
    assert str(request.url) == f"{LLM}/v1/converse"
    assert request.method == "POST"
    assert request.headers["x-model-route"] == "fast"
    assert request.headers["x-demo-scenario"] == "S0"
    assert request.headers["x-agent-id"] == "shopper"
    assert request.headers["x-obo-sub"] == "alex.rivera"
    body = json.loads(request.content)
    assert "tools" not in body
    assert body["system"] == "You are the Copperline shopper agent."
    assert body["max_tokens"] == 400
    assert body["messages"][0]["role"] == "user"
    roles = [m["role"] for m in body["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))     # chat_to_converse merges same-role turns
    assert "where is my order CO-1042" in converse_text(body)
    assert (step.kind, step.target, step.status, step.blocked_by) == ("model", f"{LLM}/v1/converse", 200, None)
    assert step.result_preview == "reply from fast"


def test_model_summary_lists_earlier_tool_results_and_denials():
    world = World()
    world.tool_text["get_reviews"] = '{"reviews": ["Great tent, stays dry."]}'
    plan = [
        {"tool": "catalog.get_reviews", "args": {"product_id": "TNT-ALPINE-2"}},
        {"tool": "refunds.issue_refund", "args": {"order_id": "CO-1077", "amount": 499}},
        {"model": {"route": "fast"}},
    ]
    run(world, scenario(plan))
    text = converse_text(json.loads(world.to("llm.test")[0].content))
    assert "Results of the tool calls you made:" in text
    assert "- catalog.get_reviews -> 200: " in text
    assert "stays dry" in text
    assert "- refunds.issue_refund -> 403: " in text
    assert "denied by policy (http 403)" in text.lower()


def test_model_summary_cuts_each_tool_result_at_1500_chars():
    world = World()
    world.tool_text["get_reviews"] = "R" * 3000
    run(world, scenario(S0_LIKE))
    text = converse_text(json.loads(world.to("llm.test")[0].content))
    assert "R" * 1500 in text
    assert "R" * 1501 not in text


def test_model_step_blocked_by_the_fabric_is_xc():
    step = run(World(), scenario([{"model": {"route": "premium"}}])).steps[0]
    assert (step.kind, step.status, step.blocked_by) == ("model", 403, "xc")


# --- http steps -------------------------------------------------------------------------------------


def test_http_step_sends_method_json_body_headers_and_bearer():
    world = World()
    body = {"po_id": "PO-5531", "carrier": "UPS", "items": []}
    plan = [{"http": {"method": "POST", "url": f"{PARTNER}/v1/asn", "body": body, "headers": {"x-test": "yes"},
                      "auth": "partner_tok"}}]
    step = run(world, scenario(plan), tokens={"partner_tok": "ptok-123"}).steps[0]
    (request,) = world.to("partner.test")
    assert request.method == "POST"
    assert json.loads(request.content) == body
    assert request.headers["content-type"].startswith("application/json")
    assert request.headers["x-test"] == "yes"
    assert request.headers["authorization"] == "Bearer ptok-123"
    assert step.kind == "http" and step.target == f"{PARTNER}/v1/asn"
    assert json.loads(step.args_preview) == body


def test_http_step_with_an_unknown_token_name_is_recorded_and_not_sent():
    world = World()
    plan = [{"http": {"method": "GET", "url": f"{PARTNER}/v1/purchase-orders", "auth": "nope"}}]
    step = run(world, scenario(plan), tokens={}).steps[0]
    assert (step.status, step.blocked_by) == (None, None)
    assert step.reason == "no token saved as nope"
    assert world.to("partner.test") == []


def test_http_connect_error_is_null_status_blocked_by_network():
    result = run(World(), scenario([{"http": {"method": "POST", "url": "http://exfil.test/drop", "body": {"rows": 1}}},
                                    {"tool": "catalog.get_product", "args": {"product_id": "X"}}]))
    exfil, after = result.steps
    assert (exfil.status, exfil.blocked_by) == (None, "network")
    assert after.status == 200                                 # the plan carries on after a network error


def test_http_step_classification_depends_on_lb_base_urls():
    plan = [{"http": {"method": "GET", "url": f"{PARTNER}/v1/internal/cost-sheet"}},
            {"http": {"method": "GET", "url": "http://elsewhere.test/forbidden"}}]
    via_lb, not_lb = run(World(), scenario(plan)).steps
    assert (via_lb.status, via_lb.blocked_by) == (403, "xc")
    assert (not_lb.status, not_lb.blocked_by) == (403, None)


# --- concurrency and stopping -----------------------------------------------------------------------


def test_real_s6_concurrent_steps_run_in_batches_and_come_back_sorted():
    world = World(partner_pages_allowed=20)
    result = run_batch(world, by_id(SCENARIOS, "S6"))
    assert len(result.steps) == 54
    assert [s.i for s in result.steps] == list(range(54))
    assert [s.n for s in result.steps] == list(range(1, 55))
    assert world.max_in_flight == 10
    pages = result.steps[4:]
    assert [s.target for s in pages] == [f"{PARTNER}/v1/purchase-orders?page={p}" for p in range(1, 51)]
    assert [s.status for s in pages] == [200] * 20 + [429] * 30
    assert all(s.blocked_by == "xc" for s in pages[20:])
    assert [(s.status, s.blocked_by) for s in result.steps[1:4]] == [(200, None), (403, "xc"), (403, "xc")]
    partner_tok = world.minted[("granite-supply", "copperline-partner")]
    assert {r.headers["authorization"] for r in world.to("partner.test")} == {f"Bearer {partner_tok}"}
    assert result.reply is None


def test_stop_on_status_stops_the_plan_early():
    world = World(adjust_allowed=2)
    plan = [
        {"tool": "inventory.adjust_stock", "repeat": 6, "stop_on_status": 429,
         "args_template": {"sku": "TNT-ALPINE-2", "store_id": "S-{n:03d}", "delta": 2}},
        {"model": {"route": "standard"}},
    ]
    result = run(world, scenario(plan, agent="storeops", persona="jordan.lee"))
    assert [s.status for s in result.steps] == [200, 200, 429]
    assert result.stopped_early is True
    assert len(world.to("tools.test")) == 3
    assert world.to("llm.test") == []


def test_real_s4_plan_without_stop_runs_every_step():
    world = World(adjust_allowed=10)
    result = run(world, by_id(SCENARIOS, "S4"), agent_id="storeops", obo_sub="jordan.lee")
    assert len(result.steps) == 41
    assert result.stopped_early is False
    tools = result.steps[1:]
    assert sum(s.status == 200 for s in tools) == 10
    assert all((s.status, s.blocked_by) == (429, "xc") for s in tools[10:])
    stores = [json.loads(r.content)["params"]["arguments"]["store_id"] for r in world.to("tools.test")]
    assert stores == [f"S-{n:03d}" for n in range(1, 41)]
    assert result.reply == by_id(SCENARIOS, "S4").final_reply


# --- previews, request ids, hook --------------------------------------------------------------------


def test_previews_are_capped_and_truncation_is_flagged():
    world = World()
    world.tool_text["get_reviews"] = "R" * (PREVIEW_LIMIT + 1000)
    plan = [{"tool": "catalog.get_reviews", "args": {"product_id": "P" * 1000}}]
    step = run(world, scenario(plan)).steps[0]
    assert PREVIEW_LIMIT == 4000
    assert len(step.args_preview) <= 300
    assert step.result_truncated is True
    assert len(step.result_preview) <= PREVIEW_LIMIT
    assert step.result_preview.startswith("R" * 100)


def test_each_step_gets_its_own_request_id_prefixed_by_the_scenario_and_sends_it():
    world = World()
    result = run(world, scenario(S0_LIKE, sid="S0"))
    ids = [s.request_id for s in result.steps]
    assert len(set(ids)) == 3
    assert all(rid.startswith("S0-") for rid in ids)
    assert [r.headers["x-request-id"] for r in world.requests] == ids


def test_step_hook_is_called_for_every_step():
    seen = []
    result = run(World(), scenario(S0_LIKE), step_hook=seen.append)
    assert len(seen) == 3
    assert all(isinstance(s, Step) for s in seen)
    assert sorted(s.i for s in seen) == [s.i for s in result.steps]


# --- final reply ------------------------------------------------------------------------------------


def test_final_reply_string_wins():
    result = run(World(), scenario(S0_LIKE, final_reply="Scripted closing line."))
    assert result.reply == "Scripted closing line."


def test_final_reply_null_uses_the_last_model_step_text():
    assert run(World(), scenario(S0_LIKE)).reply == "reply from fast"


@pytest.mark.parametrize("plan", [
    [{"tool": "catalog.get_product", "args": {"product_id": "X"}}],
    [{"model": {"route": "fast"}}, {"tool": "catalog.get_product", "args": {"product_id": "X"}}],
])
def test_final_reply_null_without_a_trailing_model_step_is_none(plan):
    assert run(World(), scenario(plan)).reply is None


# --- plan errors ------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad_step", [
    {"http": {"method": "GET", "url": "${NOT_SET_ANYWHERE}/x"}},
    {"tool": "catalog.get_product", "model": {"route": "fast"}},
])
def test_bad_plan_raises_plan_error_before_any_request(bad_step):
    world = World()
    plan = [{"tool": "catalog.get_product", "args": {"product_id": "X"}}, bad_step]
    with pytest.raises(PlanError):
        run(world, scenario(plan))
    assert world.requests == []
