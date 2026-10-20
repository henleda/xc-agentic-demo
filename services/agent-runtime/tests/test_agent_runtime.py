"""agent-runtime unit tests. The idp, llm-lb and tools-lb are replaced by one fake transport, so each test controls
exactly what the agent sees. End-to-end behavior through the real gateways is in
deploy/local/local-gateway/tests/test_agents_through_front_doors.py."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from agent_runtime.agent import NO_REPLY, Agent, AuthError
from agent_runtime.app import create_app
from agent_runtime.batch import TRACE_PREFIX, run_batch
from agent_runtime.settings import AgentConfig, Settings
from copperline_common.jwt import sign, unverified_claims
from copperline_common.personas import Personas
from copperline_common.scenarios import load_scenarios
from copperline_common.tool_schemas import load_tools
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[3]
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TOOLS = load_tools(REPO / "config/tools/tools.yaml")
PERSONAS = Personas.load(REPO / "config/personas.yaml")


def user_token(sub="alex.rivera", role="customer") -> str:
    return sign({"sub": sub, "role": role, "aud": "copperline-shop"}, KEY, ttl_s=1800, issuer="https://idp.test")


def agent_token(aud: str, obo_sub="alex.rivera", ttl=300) -> str:
    claims = {"sub": "shopper", "agent_id": "shopper", "agent_role": "shopper", "obo_sub": obo_sub,
              "obo_role": "customer", "aud": aud}
    return sign(claims, KEY, ttl_s=ttl, issuer="https://idp.test")


def settings(agent_id="shopper", **overrides) -> Settings:
    values = dict(agent=AgentConfig.load(REPO / f"config/agents/{agent_id}.yaml"), client_secret="s",
                  tools_base_url="http://tools", llm_base_url="http://llm", idp_base_url="http://idp",
                  config_dir=REPO / "config", scenarios_dir=REPO / "scenarios")
    values.update(overrides)
    return Settings(**values)


class Fake(httpx.AsyncBaseTransport):
    """idp: token exchange (or a refusal); llm: model replies popped from a list; tools: MCP results by tool name."""

    def __init__(self, model_replies=None, tool_status=None, exchange_status=200) -> None:
        self.model_replies = list(model_replies or [])
        self.tool_status = tool_status or {}
        self.exchange_status = exchange_status
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        host = request.url.host
        if host == "idp":
            if self.exchange_status != 200:
                return httpx.Response(self.exchange_status, json={"error": "unauthorized_user_role"},
                                      headers={"x-copperline-origin": "idp"})
            aud = dict(x.split("=") for x in request.content.decode().split("&"))["requested_audience"]
            return httpx.Response(200, json={"access_token": agent_token(aud)}, headers={"x-copperline-origin": "idp"})
        if host == "llm":
            reply = self.model_replies.pop(0) if self.model_replies else text_reply("done")
            return httpx.Response(200, json=reply, headers={"x-copperline-origin": "ai-gw"})
        if host == "tools":
            name = request.headers["mcp-name"]
            status = self.tool_status.get(name, 200)
            if status != 200:
                return httpx.Response(status, json={"error": "forbidden"})  # no origin header: the fabric said no
            result = {"content": [{"type": "text", "text": json.dumps({"ok": name})}], "isError": False}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result},
                                  headers={"x-copperline-origin": "mcp"})
        raise httpx.ConnectError("no such host", request=request)


def text_reply(text):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stop_reason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "route": "fast", "model_id": "fake"}


def tool_reply(name, args=None, use_id="u1"):
    content = [{"toolUse": {"toolUseId": use_id, "name": name, "input": args or {}}}]
    return {"output": {"message": {"role": "assistant", "content": content}}, "stop_reason": "tool_use",
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "route": "fast", "model_id": "fake"}


def make_agent(fake: Fake, **overrides) -> Agent:
    client = httpx.AsyncClient(transport=fake)
    return Agent(settings(**overrides), tools=TOOLS, scenarios=load_scenarios(REPO / "scenarios"), personas=PERSONAS,
                 http=client, model_http=client)


def chat(agent, message="What is the status of my order?", token=None):
    return asyncio.run(agent.handle_chat(token or user_token(), [{"role": "user", "content": message}]))


# ---- the normal loop ----------------------------------------------------------------------------------------------


def test_the_loop_stops_at_max_steps_model_calls():
    fake = Fake(model_replies=[tool_reply("orders__get_order_status", use_id=f"u{i}") for i in range(20)])
    result = chat(make_agent(fake))
    model_calls = [s for s in result.trace if s["kind"] == "model"]
    assert len(model_calls) == 8  # shopper max_steps
    assert "step limit" in result.reply


def test_a_tool_the_model_invents_is_never_called():
    fake = Fake(model_replies=[tool_reply("inventory__adjust_stock"), text_reply("ok")])  # not declared by shopper
    result = chat(make_agent(fake))
    step = next(s for s in result.trace if s["kind"] == "tool")
    assert (step["tool"], step["status"], step["reason"]) == ("inventory.adjust_stock", None, "not a declared tool")
    assert not [r for r in fake.requests if r.url.host == "tools"]


def test_a_denied_tool_is_fed_back_as_one_line_and_marked_error():
    fake = Fake(model_replies=[tool_reply("refunds__issue_refund", {"order_id": "CO-1042", "amount": 1}),
                               text_reply("I can't do that.")], tool_status={"issue_refund": 403})
    result = chat(make_agent(fake))
    assert result.reply == "I can't do that."
    last_model_request = [r for r in fake.requests if r.url.host == "llm"][-1]
    sent = json.loads(last_model_request.content)
    tool_result = sent["messages"][-1]["content"][0]["toolResult"]
    assert tool_result["status"] == "error"
    assert tool_result["content"] == [{"text": "Tool call denied by policy (HTTP 403)."}]


def test_every_outbound_call_carries_identity_headers_and_a_request_id():
    fake = Fake(model_replies=[tool_reply("orders__get_order_status"), text_reply("shipped")])
    chat(make_agent(fake))
    for request in fake.requests:
        if request.url.host == "idp":
            continue
        assert request.headers["x-agent-id"] == "shopper" and request.headers["x-obo-sub"] == "alex.rivera"
        assert request.headers["x-request-id"].startswith("shopper-")
    model = next(r for r in fake.requests if r.url.host == "llm")
    assert model.headers["x-model-route"] == "fast" and "x-demo-scenario" not in model.headers


def test_the_model_is_offered_exactly_the_declared_tools_including_the_overdeclared_one():
    fake = Fake(model_replies=[text_reply("hi")])
    chat(make_agent(fake))
    sent = json.loads(next(r for r in fake.requests if r.url.host == "llm").content)
    assert [t["name"] for t in sent["tools"]] == ["catalog__search_products", "catalog__get_product",
                                                  "catalog__get_reviews", "orders__get_order_status",
                                                  "refunds__issue_refund"]


def test_a_model_failure_ends_the_turn_with_a_polite_reply():
    class Down(Fake):
        async def handle_async_request(self, request):
            if request.url.host == "llm":
                return httpx.Response(503, json={"error": "x"})
            return await super().handle_async_request(request)
    result = chat(make_agent(Down()))
    assert result.reply == NO_REPLY
    assert result.trace[-1]["kind"] == "model" and result.trace[-1]["status"] == 503


# ---- tokens -------------------------------------------------------------------------------------------------------


def test_a_refused_exchange_is_a_trace_step_and_the_calls_go_out_without_a_token():
    fake = Fake(exchange_status=400, model_replies=[text_reply("hi")])
    result = chat(make_agent(fake))
    tokens = [s for s in result.trace if s["kind"] == "token"]
    assert [(s["status"], s["blocked_by"], s["identity"]) for s in tokens] == [(400, "server", None)] * 2
    model = next(r for r in fake.requests if r.url.host == "llm")
    assert "authorization" not in model.headers  # llm-lb will then 401 it, visibly


def test_tokens_are_cached_per_human_not_shared():
    fake = Fake(model_replies=[text_reply("a"), text_reply("b"), text_reply("c")])
    agent = make_agent(fake)
    chat(agent, token=user_token("alex.rivera"))
    chat(agent, token=user_token("alex.rivera"))
    chat(agent, token=user_token("mallory.x"))
    exchanges = [r for r in fake.requests if r.url.host == "idp"]
    assert len(exchanges) == 4  # alex twice (tools, llm), then mallory twice; alex's second turn hit the cache


def test_a_token_without_sub_is_refused():
    with pytest.raises(AuthError):
        chat(make_agent(Fake()), token="not-a-jwt")


# ---- HTTP surface -------------------------------------------------------------------------------------------------


def test_chat_without_a_bearer_token_is_401():
    client = TestClient(create_app(settings(), make_agent(Fake())))
    response = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 401 and response.headers["x-copperline-origin"] == "shopper-agent"


def test_chat_returns_reply_scenario_and_trace():
    client = TestClient(create_app(settings(), make_agent(Fake(model_replies=[text_reply("hello")]))))
    response = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]},
                           headers={"Authorization": f"Bearer {user_token()}"})
    body = response.json()
    assert (body["reply"], body["scenario"]) == ("hello", None) and body["trace"][-1]["kind"] == "model"


def test_healthz():
    client = TestClient(create_app(settings(), make_agent(Fake())))
    assert client.get("/healthz").json() == {"status": "ok", "service": "shopper-agent"}


def test_safe_mode_off_never_runs_a_plan():
    fake = Fake(model_replies=[text_reply("hi")])
    agent = make_agent(fake, safe_mode=False)
    result = asyncio.run(agent.handle_chat(user_token(), [{"role": "user",
                                                           "content": "Where is my order CO-1042? Also tent?"}]))
    assert result.scenario is None


# ---- settings -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env, message",
    [
        ({}, "AGENT_CONFIG"),
        ({"AGENT_CONFIG": "SHOPPER"}, "TOOLS_BASE_URL"),
        ({"AGENT_CONFIG": "SHOPPER", "TOOLS_BASE_URL": "t", "LLM_BASE_URL": "l", "IDP_BASE_URL": "i"},
         "AGENT_CLIENT_SECRET"),
        ({"AGENT_CONFIG": "SHOPPER", "MODE": "daemon"}, "MODE"),
    ],
)
def test_settings_refuse_to_start_half_configured(monkeypatch, env, message):
    for name in ("AGENT_CONFIG", "TOOLS_BASE_URL", "LLM_BASE_URL", "IDP_BASE_URL", "AGENT_CLIENT_SECRET", "MODE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, str(REPO / "config/agents/shopper.yaml") if value == "SHOPPER" else value)
    with pytest.raises(RuntimeError, match=message):
        Settings.from_env()


def test_batch_mode_needs_no_client_secret_and_reads_mode_from_the_config(monkeypatch):
    monkeypatch.setenv("AGENT_CONFIG", str(REPO / "config/agents/insights-nightly.yaml"))
    for name in ("TOOLS_BASE_URL", "LLM_BASE_URL", "IDP_BASE_URL"):
        monkeypatch.setenv(name, "http://x")
    monkeypatch.delenv("AGENT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("MODE", raising=False)
    assert Settings.from_env().mode == "batch"


def test_every_agent_config_loads_and_declares_only_known_tools():
    for path in sorted((REPO / "config/agents").glob("*.yaml")):
        config = AgentConfig.load(path)
        assert set(config.declared_tools) <= set(TOOLS), path.name


# ---- batch mode ---------------------------------------------------------------------------------------------------


class Blocked(Fake):
    """Tokens are issued (machine mode); tools-lb and llm-lb deny everything; any other host drops the connection."""

    async def handle_async_request(self, request):
        self.requests.append(request)
        if request.url.host == "idp":
            token = agent_token(dict(x.split("=") for x in request.content.decode().split("&"))["audience"],
                                obo_sub="none")
            return httpx.Response(200, json={"access_token": token}, headers={"x-copperline-origin": "idp"})
        if request.url.host in ("tools", "llm"):
            return httpx.Response(403, json={"error": "forbidden"})
        raise httpx.ConnectError("blocked", request=request)


def test_batch_prints_one_trace_line_per_step_and_exits_0_even_when_everything_is_blocked(capsys, monkeypatch):
    monkeypatch.setenv("AGENT_STOREOPS_SECRET", "s")
    monkeypatch.setenv("EXFIL_TEST_URL", "https://exfil.invalid/rows")  # the container gets it from its env

    s = settings("insights-nightly", mode="batch", client_secret=None)
    code = asyncio.run(run_batch(s, http=httpx.AsyncClient(transport=Blocked())))
    lines = [json.loads(line[len(TRACE_PREFIX):]) for line in capsys.readouterr().out.splitlines()
             if line.startswith(TRACE_PREFIX)]
    assert code == 0
    assert [s["i"] for s in lines] == list(range(6))
    assert [(s["status"], s["blocked_by"]) for s in lines][2:] == [(403, "xc"), (403, "xc"), (None, "network"),
                                                                   (403, "xc")]


def test_batch_without_a_job_scenario_exits_2(tmp_path):
    s = settings("insights-nightly", mode="batch", scenarios_dir=tmp_path)
    assert asyncio.run(run_batch(s, http=httpx.AsyncClient(transport=Fake()))) == 2


def test_batch_with_an_unresolvable_plan_exits_2_before_any_step(capsys, monkeypatch):
    """S5 needs ${EXFIL_TEST_URL}; without it the plan never starts (no TRACE line, no request)."""
    monkeypatch.delenv("EXFIL_TEST_URL", raising=False)
    fake = Blocked()
    code = asyncio.run(run_batch(settings("insights-nightly", mode="batch", client_secret=None),
                                 http=httpx.AsyncClient(transport=fake)))
    assert code == 2 and fake.requests == []
    assert TRACE_PREFIX not in capsys.readouterr().out


def test_s5_runs_with_exactly_the_env_the_vk8s_cronjob_gives_it(capsys, monkeypatch):
    """The CronJob container env plus the runtime-env ConfigMap, read from the manifests, is all the job gets.
    A variable the plan needs but the manifest lacks fails here, not on stage (the step-7 review found
    EXFIL_TEST_URL missing). ${...} placeholders stand for what render_manifests.py substitutes from .env."""
    import yaml

    cronjob = yaml.safe_load((REPO / "deploy/vk8s/40-insights-cronjob.yaml").read_text())
    container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    config_maps = {doc["metadata"]["name"]: doc["data"]
                   for doc in yaml.safe_load_all((REPO / "deploy/vk8s/00-config.yaml").read_text())}
    env = {}
    for source in container.get("envFrom", []):
        env.update(config_maps[source["configMapRef"]["name"]])
    for var in container["env"]:
        value = var.get("value", "secret-from-agent-credentials")
        env[var["name"]] = "https://rendered.example/exfil" if value == "${EXFIL_TEST_URL}" else value
    assert "${" not in "".join(env.values()), f"unrendered placeholder in the CronJob env: {env}"

    import os
    for name in list(os.environ):
        if name not in ("PATH", "HOME"):
            monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENT_CONFIG", str(REPO / "config/agents/insights-nightly.yaml"))  # the ConfigMap mount
    monkeypatch.setenv("SCENARIOS_DIR", str(REPO / "scenarios"))  # baked into the image
    s = Settings.from_env()
    assert s.mode == "batch"
    code = asyncio.run(run_batch(s, http=httpx.AsyncClient(transport=Blocked())))
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith(TRACE_PREFIX)]
    assert code == 0 and len(lines) == 6


def flat_config(tmp_path) -> Path:
    """The flat /config layout the container sees (the vK8s ConfigMap)."""
    import shutil

    for source in ("config/tools/tools.yaml", "config/personas.yaml", "config/agents/shopper.yaml"):
        shutil.copy(REPO / source, tmp_path)
    return tmp_path


def test_batch_and_plan_clients_fail_a_dropped_connect_fast(tmp_path):
    """The model read may take 65 s, but a silently dropped connect (S5's exfil behind a network policy) must not."""
    from agent_runtime.agent import CONNECT_TIMEOUT_S
    from agent_runtime.app import build_agent

    agent = build_agent(settings(config_dir=flat_config(tmp_path)))
    assert agent.model_http.timeout.connect == CONNECT_TIMEOUT_S <= 10
    assert agent.model_http.timeout.read == 65.0
    assert agent.http.timeout.read == 10.0


# ---- step-7 review: tokens, tool loop, scripted turns ---------------------------------------------------------------


def llm_requests(fake):
    return [r for r in fake.requests if r.url.host == "llm"]


def test_a_cache_hit_really_sends_the_cached_tokens():
    fake = Fake(model_replies=[text_reply("a"), text_reply("b")])
    agent = make_agent(fake)
    chat(agent)
    first_bearer = llm_requests(fake)[0].headers["authorization"]
    chat(agent)
    assert llm_requests(fake)[1].headers["authorization"] == first_bearer
    assert len([r for r in fake.requests if r.url.host == "idp"]) == 2


def test_a_token_near_expiry_is_exchanged_again_before_the_turn():
    """Within TOKEN_REFRESH_MARGIN_S of exp the cached token is not served, so it cannot expire mid-turn."""
    from agent_runtime.agent import TOKEN_REFRESH_MARGIN_S
    from copperline_common.tokens import TokenStore

    now = [1_000_000.0]
    fake = Fake(model_replies=[text_reply("a"), text_reply("b"), text_reply("c")])
    client = httpx.AsyncClient(transport=fake)
    agent = Agent(settings(), tools=TOOLS, scenarios=load_scenarios(REPO / "scenarios"), personas=PERSONAS,
                  http=client, model_http=client, token_store=TokenStore(skew_s=TOKEN_REFRESH_MARGIN_S,
                                                                         clock=lambda: now[0]))
    assert agent.tokens.skew_s == TOKEN_REFRESH_MARGIN_S >= 120
    # Fake tokens carry a real exp (300 s from the wall clock); pin the store's clock relative to it.
    chat(agent)
    exp = unverified_claims(llm_requests(fake)[0].headers["authorization"].removeprefix("Bearer "))["exp"]
    now[0] = exp - TOKEN_REFRESH_MARGIN_S - 1  # still has more than the margin left: cache hit
    chat(agent)
    assert len([r for r in fake.requests if r.url.host == "idp"]) == 2
    now[0] = exp - TOKEN_REFRESH_MARGIN_S + 1  # less than the margin left: exchange again
    result = chat(agent)
    assert len([r for r in fake.requests if r.url.host == "idp"]) == 4
    assert [s["kind"] for s in result.trace[:2]] == ["token", "token"]


def test_the_default_store_uses_the_turn_margin():
    from agent_runtime.agent import TOKEN_REFRESH_MARGIN_S

    assert make_agent(Fake()).tokens.skew_s == TOKEN_REFRESH_MARGIN_S


def test_two_tool_uses_in_one_reply_both_run_and_are_answered_in_order():
    two = {"output": {"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "u1", "name": "catalog__get_reviews", "input": {"product_id": "TNT-ALPINE-2"}}},
        {"toolUse": {"toolUseId": "u2", "name": "orders__get_order_status", "input": {"order_id": "CO-1042"}}}]}},
        "stop_reason": "tool_use", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}
    fake = Fake(model_replies=[two, text_reply("both done")])
    result = chat(make_agent(fake))
    assert [s["tool"] for s in result.trace if s["kind"] == "tool"] == ["catalog.get_reviews",
                                                                        "orders.get_order_status"]
    follow_up = json.loads(llm_requests(fake)[1].content)["messages"]
    assert follow_up[-2] == two["output"]["message"]  # the assistant turn with both toolUse blocks comes first
    assert [b["toolResult"]["toolUseId"] for b in follow_up[-1]["content"]] == ["u1", "u2"]
    assert result.reply == "both done"


def test_an_undeclared_tool_and_an_mcp_error_go_back_as_status_error():
    class McpError(Fake):
        async def handle_async_request(self, request):
            if request.url.host == "tools":
                self.requests.append(request)
                result = {"content": [{"type": "text", "text": "order not found"}], "isError": True}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result},
                                      headers={"x-copperline-origin": "mcp"})
            return await super().handle_async_request(request)

    two = {"output": {"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "u1", "name": "inventory__adjust_stock", "input": {}}},
        {"toolUse": {"toolUseId": "u2", "name": "orders__get_order_status", "input": {"order_id": "X"}}}]}},
        "stop_reason": "tool_use", "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}
    fake = McpError(model_replies=[two, text_reply("ok")])
    chat(make_agent(fake))
    results = json.loads(llm_requests(fake)[1].content)["messages"][-1]["content"]
    assert [b["toolResult"]["status"] for b in results] == ["error", "error"]
    assert results[1]["toolResult"]["content"] == [{"text": "order not found"}]


def run_s3(fake, persona="sam.patel", role="care_agent"):
    agent = make_agent(fake, agent=AgentConfig.load(REPO / "config/agents/refunds.yaml"))
    message = "Refund $120 on CO-1042, item arrived damaged."
    return asyncio.run(agent.handle_chat(user_token(persona, role), [{"role": "user", "content": message}]))


def test_the_closing_call_has_no_tools_carries_the_scenario_and_the_tool_outcomes():
    """S3a: get_order_details succeeds, issue_refund is denied; the closing call must be told exactly that."""
    fake = Fake(tool_status={"issue_refund": 403}, model_replies=[text_reply("Denied, please escalate.")])
    result = run_s3(fake)
    closing = llm_requests(fake)[-1]
    body = json.loads(closing.content)
    assert result.scenario == "S3a" and result.reply == "Denied, please escalate."
    assert "tools" not in body and closing.headers["x-demo-scenario"] == "S3a"
    summary = body["messages"][-1]["content"][-1]["text"]
    assert "refunds.issue_refund -> 403: Tool call denied by policy (HTTP 403)." in summary
    assert "orders.get_order_details -> 200:" in summary


def test_the_closing_summary_reports_an_mcp_error_as_its_text_not_success():
    class RefundError(Fake):
        async def handle_async_request(self, request):
            if request.url.host == "tools" and request.headers["mcp-name"] == "issue_refund":
                self.requests.append(request)
                result = {"content": [{"type": "text", "text": "order not found"}], "isError": True}
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": result},
                                      headers={"x-copperline-origin": "mcp"})
            return await super().handle_async_request(request)

    fake = RefundError()
    run_s3(fake, "morgan.chen", "care_supervisor")
    summary = json.loads(llm_requests(fake)[-1].content)["messages"][-1]["content"][-1]["text"]
    assert "refunds.issue_refund -> 200: order not found" in summary
    assert "succeeded" not in summary


def test_no_closing_call_after_the_plans_own_model_step_failed():
    """S0 ends with a model step; if it fails, the turn ends there instead of calling the model again."""
    class ModelDown(Fake):
        async def handle_async_request(self, request):
            if request.url.host == "llm":
                self.requests.append(request)
                return httpx.Response(504, json={"error": "bedrock_timeout"}, headers={"x-copperline-origin": "ai-gw"})
            return await super().handle_async_request(request)

    fake = ModelDown()
    message = "Hi, where is my order CO-1042? Also, is the Alpine 2 tent any good?"
    result = asyncio.run(make_agent(fake).handle_chat(user_token(), [{"role": "user", "content": message}]))
    assert result.scenario == "S0" and result.reply == NO_REPLY
    assert len(llm_requests(fake)) == 1


# ---- step-7 review: HTTP surface, logs, startup ----------------------------------------------------------------------


def test_a_lone_surrogate_is_400_before_any_plan_step_runs():
    fake = Fake()
    client = TestClient(create_app(settings(), make_agent(fake)))
    response = client.post("/v1/chat", content=b'{"messages":[{"role":"user","content":"hello \\ud800"}]}',
                           headers={"Authorization": f"Bearer {user_token()}", "content-type": "application/json"})
    assert response.status_code == 400 and response.json()["error"] == "bad_request"
    assert fake.requests == []


def test_agent_log_lines_carry_the_agent_id_and_unscripted_steps_are_logged(capsys):
    fake = Fake(model_replies=[tool_reply("orders__get_order_status"), text_reply("shipped")])
    client = TestClient(create_app(settings(), make_agent(fake)))
    capsys.readouterr()
    client.post("/v1/chat", json={"messages": [{"role": "user", "content": "status?"}]},
                headers={"Authorization": f"Bearer {user_token()}"})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    steps = [line for line in lines if line["event"] == "plan_step"]
    assert [s["kind"] for s in steps] == ["token", "token", "model", "tool", "model"]
    assert {line["agent_id"] for line in lines if line["event"] in ("plan_step", "chat_done")} == {"shopper"}


@pytest.mark.parametrize("problem", ["no tools.yaml", "bad yaml"])
def test_startup_problems_are_one_json_line_and_exit_2(tmp_path, monkeypatch, capsys, problem):
    import shutil

    from agent_runtime import __main__ as main

    shutil.copy(REPO / "config/agents/shopper.yaml", tmp_path / "shopper.yaml")
    shutil.copy(REPO / "config/personas.yaml", tmp_path)
    if problem == "bad yaml":
        shutil.copy(REPO / "config/tools/tools.yaml", tmp_path)
        (tmp_path / "shopper.yaml").write_text("agent_id: [unclosed\n")
    for name, value in {"AGENT_CONFIG": str(tmp_path / "shopper.yaml"), "TOOLS_BASE_URL": "http://t",
                        "LLM_BASE_URL": "http://l", "IDP_BASE_URL": "http://i", "AGENT_CLIENT_SECRET": "s",
                        "SCENARIOS_DIR": str(REPO / "scenarios")}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MODE", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        main.main()
    assert exit_info.value.code == 2
    errors = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"agent_config_error"' in line]
    assert len(errors) == 1
