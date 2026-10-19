"""ai-gw contract tests (docs/03 "copperline-ai-gw", services/ai-gw/README.md). Nothing here calls AWS: Bedrock mode
is exercised with botocore's Stubber, which checks each request against the real Bedrock API model."""

import boto3
import pytest
from ai_gw.app import create_admin_app, create_gateway_app
from ai_gw.models import BedrockModel, StubModel
from ai_gw.settings import Settings
from ai_gw.usage import Usage
from botocore.stub import Stubber
from fastapi.testclient import TestClient

KEY = "test-gateway-key"
USER = {"role": "user", "content": [{"text": "Where is my order CO-1042?"}]}
BODY = {"messages": [USER], "max_tokens": 100}


def settings(**overrides) -> Settings:
    values = dict(stub_model=True, route_models={}, budgets={"fast": 1000, "standard": 1000, "premium": 1000},
                  api_key=KEY, require_key=True)
    values.update(overrides)
    result = Settings(**values)
    result.validate()
    return result


@pytest.fixture
def gw():
    """gw(**settings) -> (gateway TestClient, admin TestClient) sharing one Usage."""
    opened = []

    def _make(model=None, usage_clock=None, **overrides):
        s = settings(**overrides)
        usage = Usage(s.budgets, clock=usage_clock) if usage_clock else Usage(s.budgets)
        pair = (TestClient(create_gateway_app(s, usage, model)), TestClient(create_admin_app(s, usage)))
        opened.extend(pair)
        return pair

    yield _make
    for client in opened:
        client.close()


def call(client, body=BODY, *, route="fast", key=KEY, headers=None):
    sent = [] if key is None else [("x-api-key", key)]
    sent += [("x-model-route", r) for r in ([route] if isinstance(route, str) else route or [])]
    sent += list((headers or {}).items())
    return client.post("/v1/converse", json=body, headers=sent)


# ---- check order ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, "wrong"])
def test_missing_or_wrong_key_is_401(gw, key):
    gateway, _ = gw()
    response = call(gateway, key=key)
    assert response.status_code == 401 and response.json()["error"] == "invalid_api_key"


def test_two_api_keys_are_401_even_if_one_is_right(gw):
    gateway, _ = gw()
    response = gateway.post("/v1/converse", json=BODY,
                            headers=[("x-api-key", "wrong"), ("x-api-key", KEY), ("x-model-route", "fast")])
    assert response.status_code == 401


def test_key_is_checked_before_route_and_body(gw):
    gateway, _ = gw()
    assert call(gateway, {"nonsense": 1}, route="bogus", key=None).status_code == 401


def test_key_check_can_be_off(gw):
    gateway, _ = gw(require_key=False, api_key=None)
    assert call(gateway, key=None).status_code == 200


@pytest.mark.parametrize("route", [None, "bogus", ["fast", "fast"], ["fast", "premium"]])
def test_route_must_be_exactly_one_known_value(gw, route):
    """A repeated x-model-route is refused (docs/03): llm-lb's policy matched one copy, and we must not use another."""
    gateway, _ = gw()
    response = call(gateway, route=route)
    assert response.status_code == 400 and response.json()["error"] == "bad_route"


def test_route_is_checked_before_body(gw):
    gateway, _ = gw()
    assert call(gateway, {"nonsense": 1}, route="bogus").json()["error"] == "bad_route"


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"messages": []},
        {"messages": [{"role": "system", "content": [{"text": "x"}]}]},
        {"messages": [{"role": "user", "content": "plain string"}]},
        {"messages": [{"role": "assistant", "content": [{"text": "hi"}]}]},
        {"messages": [USER], "system": ["not", "a", "string"]},
        {"messages": [USER], "tools": [{"name": "x"}]},
        {"messages": [USER], "max_tokens": 0},
        {"messages": [USER], "max_tokens": True},
    ],
)
def test_malformed_bodies_are_400(gw, body):
    gateway, _ = gw()
    response = call(gateway, body)
    assert response.status_code == 400 and response.json()["error"] == "bad_request"


def test_non_json_body_is_400(gw):
    gateway, _ = gw()
    response = gateway.post("/v1/converse", content=b"{", headers={"x-api-key": KEY, "x-model-route": "fast"})
    assert response.status_code == 400 and response.json()["error"] == "bad_request"


def test_only_converse_is_served_on_the_gateway_port(gw):
    gateway, _ = gw()
    assert gateway.get("/v1/usage").status_code == 404
    assert gateway.get("/v1/converse").status_code == 405
    assert gateway.get("/healthz").json() == {"status": "ok", "service": "ai-gw"}


def test_every_response_says_it_came_from_ai_gw(gw):
    gateway, _ = gw()
    for response in (call(gateway), call(gateway, key=None), call(gateway, route="bogus")):
        assert response.headers["x-copperline-origin"] == "ai-gw"


# ---- budgets and usage --------------------------------------------------------------------------------------------


def test_budget_is_per_route_and_429_once_used_up(gw):
    gateway, admin = gw(budgets={"fast": 10, "standard": 1000, "premium": 1000})
    first = call(gateway, headers={"x-agent-id": "shopper"})
    assert first.status_code == 200 and first.json()["usage"]["totalTokens"] >= 10  # one call uses the whole budget
    over = call(gateway, headers={"x-agent-id": "shopper"})
    assert over.status_code == 429 and over.json()["error"] == "budget_exceeded"
    assert call(gateway, route="standard").status_code == 200  # other routes keep their own budget
    snapshot = admin.get("/v1/usage").json()
    assert snapshot["rejected_budget"] == 1 and snapshot["calls"] == 2
    assert snapshot["budgets"]["fast"] == {"limit": 10, "used": first.json()["usage"]["totalTokens"]}


def test_usage_is_counted_by_route_and_agent(gw):
    gateway, admin = gw()
    call(gateway, headers={"x-agent-id": "shopper"})
    call(gateway, headers={"x-agent-id": "shopper"})
    call(gateway, route="standard", headers={"x-agent-id": "storeops"})
    call(gateway)  # no agent header
    usage = admin.get("/v1/usage").json()
    assert usage["mode"] == "stub"
    assert usage["by_agent"]["shopper"]["calls"] == 2 and usage["by_agent"]["storeops"]["calls"] == 1
    assert usage["by_agent"]["unknown"]["calls"] == 1
    assert usage["by_route"]["fast"]["calls"] == 3 and usage["by_route"]["standard"]["calls"] == 1
    row = usage["by_route"]["fast"]
    assert row["total"] == row["input"] + row["output"] > 0


def test_refused_calls_do_not_count(gw):
    gateway, admin = gw()
    call(gateway, key=None)
    call(gateway, route="bogus")
    assert admin.get("/v1/usage").json()["calls"] == 0


def test_reset_clears_usage_and_budgets(gw):
    gateway, admin = gw(budgets={"fast": 1, "standard": 1, "premium": 1})
    call(gateway)
    assert call(gateway).status_code == 429
    assert admin.post("/v1/usage/reset").json() == {"status": "reset"}
    assert admin.get("/v1/usage").json()["calls"] == 0
    assert call(gateway).status_code == 200


def test_budget_day_rolls_over_at_utc_midnight(gw):
    day = ["2026-10-19"]
    gateway, _ = gw(budgets={"fast": 1, "standard": 1, "premium": 1}, usage_clock=lambda: day[0])
    call(gateway)
    assert call(gateway).status_code == 429
    day[0] = "2026-10-20"
    assert call(gateway).status_code == 200


def test_admin_port_serves_only_usage(gw):
    _, admin = gw()
    assert admin.post("/v1/converse", json=BODY).status_code == 404
    assert admin.get("/healthz").json() == {"status": "ok", "service": "ai-gw-admin"}


# ---- stub mode ----------------------------------------------------------------------------------------------------


def test_stub_reply_is_keyed_on_x_demo_scenario(gw):
    gateway, _ = gw()
    s0 = call(gateway, headers={"x-demo-scenario": "S0"}).json()
    other = call(gateway, headers={"x-demo-scenario": "nope"}).json()
    assert "CO-1042" in s0["output"]["message"]["content"][0]["text"]
    assert other["output"]["message"]["content"][0]["text"] == StubModel().default
    assert s0["model_id"] == "stub" and s0["route"] == "fast" and s0["stop_reason"] == "end_turn"


def test_every_scenario_with_a_model_step_has_a_stub_reply():
    from pathlib import Path

    from copperline_common.scenarios import load_scenarios

    repo = Path(__file__).resolve().parents[3]
    with_model = {s.id for s in load_scenarios(repo / "scenarios") if any("model" in step for step in s.plan)}
    assert with_model and with_model <= set(StubModel().scenarios)


def test_stub_calls_a_matching_tool_when_tools_are_offered(gw):
    """Enough of a tool loop for agent-runtime to be tested offline (step 7)."""
    gateway, _ = gw()
    tools = [{"name": "catalog__get_reviews", "description": "", "input_schema": {"type": "object"}},
             {"name": "orders__get_order_status", "description": "", "input_schema": {"type": "object"}}]
    first = call(gateway, {"messages": [USER], "tools": tools}).json()
    block = first["output"]["message"]["content"][0]["toolUse"]
    assert block["name"] == "orders__get_order_status" and first["stop_reason"] == "tool_use"

    answered = {"messages": [USER, {"role": "assistant", "content": [{"toolUse": block}]},
                             {"role": "user", "content": [{"toolResult": {"toolUseId": block["toolUseId"],
                                                                          "content": [{"text": "shipped"}]}}]}],
                "tools": tools}
    second = call(gateway, answered).json()
    assert second["stop_reason"] == "end_turn" and "text" in second["output"]["message"]["content"][0]


def test_stub_never_calls_a_tool_on_a_scripted_turn(gw):
    gateway, _ = gw()
    tools = [{"name": "orders__get_order_status", "description": "", "input_schema": {"type": "object"}}]
    reply = call(gateway, {"messages": [USER], "tools": tools}, headers={"x-demo-scenario": "S0"}).json()
    assert "text" in reply["output"]["message"]["content"][0]


# ---- Bedrock mode (botocore Stubber; no AWS) ---------------------------------------------------------------------


@pytest.fixture
def bedrock():
    client = boto3.client("bedrock-runtime", region_name="us-east-1", aws_access_key_id="x",
                          aws_secret_access_key="x")
    with Stubber(client) as stubber:
        yield client, stubber
    stubber.assert_no_pending_responses()


BEDROCK_REPLY = {
    "output": {"message": {"role": "assistant", "content": [{"text": "It shipped."}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15},
    "metrics": {"latencyMs": 100},
}


def test_bedrock_request_maps_route_system_tools_and_cap(gw, bedrock):
    client, stubber = bedrock
    tools = [{"name": "orders__get_order_status", "description": "Order status", "input_schema": {"type": "object"}}]
    stubber.add_response("converse", BEDROCK_REPLY, {
        "modelId": "model-standard",
        "messages": [USER],
        "system": [{"text": "Be brief."}],
        "inferenceConfig": {"maxTokens": 2048},  # 5000 asked, capped
        "toolConfig": {"tools": [{"toolSpec": {"name": "orders__get_order_status", "description": "Order status",
                                               "inputSchema": {"json": {"type": "object"}}}}]},
    })
    model = BedrockModel("us-east-1", {"standard": "model-standard"}, client=client)
    gateway, admin = gw(model=model, stub_model=False, aws_region="us-east-1",
                        route_models={"standard": "model-standard"})
    response = call(gateway, {"messages": [USER], "system": "Be brief.", "tools": tools, "max_tokens": 5000},
                    route="standard", headers={"x-agent-id": "refunds"})
    assert response.status_code == 200
    assert response.json() == {"output": BEDROCK_REPLY["output"], "stop_reason": "end_turn",
                               "usage": {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15},
                               "route": "standard", "model_id": "model-standard"}
    assert admin.get("/v1/usage").json()["by_agent"]["refunds"]["total"] == 15


def test_a_route_without_a_model_is_refused_in_bedrock_mode(gw, bedrock):
    client, _ = bedrock
    model = BedrockModel("us-east-1", {"standard": "model-standard"}, client=client)
    gateway, _ = gw(model=model, stub_model=False, aws_region="us-east-1",
                    route_models={"standard": "model-standard"})
    assert call(gateway, route="premium").json()["error"] == "bad_route"


@pytest.mark.parametrize("code, status", [("ThrottlingException", 429), ("AccessDeniedException", 502),
                                          ("ValidationException", 502)])
def test_bedrock_errors_map_to_gateway_statuses(gw, bedrock, code, status):
    client, stubber = bedrock
    stubber.add_client_error("converse", service_error_code=code, service_message="nope")
    model = BedrockModel("us-east-1", {"fast": "model-fast"}, client=client)
    gateway, admin = gw(model=model, stub_model=False, aws_region="us-east-1", route_models={"fast": "model-fast"})
    response = call(gateway)
    assert response.status_code == status and code in response.json()["message"]
    assert admin.get("/v1/usage").json()["calls"] == 0


# ---- settings -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env, message",
    [
        ({"DEMO_STUB_MODEL": "true", "GATEWAY_REQUIRE_KEY": "true"}, "GATEWAY_API_KEY"),
        ({"DEMO_STUB_MODEL": "false", "GATEWAY_API_KEY": "k"}, "AWS_REGION"),
        ({"DEMO_STUB_MODEL": "false", "GATEWAY_API_KEY": "k", "AWS_REGION": "us-east-1"}, "ROUTE_"),
        ({"DEMO_STUB_MODEL": "maybe", "GATEWAY_API_KEY": "k"}, "DEMO_STUB_MODEL"),
        ({"DEMO_STUB_MODEL": "true", "GATEWAY_API_KEY": "k", "BUDGET_TOKENS_FAST": "lots"}, "BUDGET_TOKENS_FAST"),
    ],
)
def test_settings_refuse_to_start_half_configured(monkeypatch, env, message):
    for name in ("DEMO_STUB_MODEL", "GATEWAY_API_KEY", "GATEWAY_REQUIRE_KEY", "AWS_REGION", "ROUTE_FAST",
                 "ROUTE_STANDARD", "ROUTE_PREMIUM", "BUDGET_TOKENS_FAST"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        Settings.from_env()


def test_settings_from_env_in_bedrock_mode(monkeypatch):
    for name, value in {"DEMO_STUB_MODEL": "false", "GATEWAY_API_KEY": "k", "AWS_REGION": "us-east-1",
                        "ROUTE_FAST": "m-fast", "ROUTE_STANDARD": "m-std", "BUDGET_TOKENS_PREMIUM": "50"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ROUTE_PREMIUM", raising=False)
    s = Settings.from_env()
    assert s.route_models == {"fast": "m-fast", "standard": "m-std"} and s.unknown_routes == ["premium"]
    assert s.budgets["premium"] == 50 and not s.stub_model
