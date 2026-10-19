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


@pytest.mark.parametrize("keys", [("wrong", KEY), (KEY, "wrong"), (KEY, KEY)])
def test_the_api_key_must_be_sent_exactly_once(gw, keys):
    """(KEY, KEY) pins "exactly once": a check that reads only the first copy would accept it."""
    gateway, _ = gw()
    headers = [("x-api-key", key) for key in keys] + [("x-model-route", "fast")]
    response = gateway.post("/v1/converse", json=BODY, headers=headers)
    assert response.status_code == 401 and response.json()["error"] == "invalid_api_key"


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


class SpyModel:
    """Wraps the stub and records each request body the gateway hands to the model."""

    def __init__(self) -> None:
        self.stub = StubModel()
        self.bodies: list[dict] = []

    def converse(self, route, body, scenario):
        self.bodies.append(body)
        return self.stub.converse(route, body, scenario)


def test_an_over_budget_call_never_reaches_the_model(gw):
    spy = SpyModel()
    gateway, _ = gw(model=spy, budgets={"fast": 1, "standard": 1, "premium": 1})
    assert call(gateway).status_code == 200
    assert call(gateway).status_code == 429
    assert len(spy.bodies) == 1


def test_a_bad_body_is_400_even_when_the_budget_is_used_up(gw):
    """Check order: body (3) before budget (4)."""
    gateway, _ = gw(budgets={"fast": 1, "standard": 1, "premium": 1})
    call(gateway)
    assert call(gateway, {"messages": []}).json()["error"] == "bad_request"


def test_max_tokens_is_capped_and_defaults_to_the_cap(gw):
    spy = SpyModel()
    gateway, _ = gw(model=spy)
    call(gateway, {"messages": [USER], "max_tokens": 400})
    call(gateway, {"messages": [USER], "max_tokens": 99999})
    call(gateway, {"messages": [USER]})
    assert [body["max_tokens"] for body in spy.bodies] == [400, 2048, 2048]


def test_deeply_nested_json_is_400_not_500(gw):
    gateway, _ = gw()
    nested = b"[" * 100_000 + b"]" * 100_000
    response = gateway.post("/v1/converse", content=nested, headers={"x-api-key": KEY, "x-model-route": "fast"})
    assert response.status_code == 400 and response.json()["error"] == "bad_request"


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


TOOL_STATUS = {"name": "orders__get_order_status", "description": "", "input_schema": {"type": "object"}}
TOOL_REVIEWS = {"name": "catalog__get_reviews", "description": "", "input_schema": {"type": "object"}}


@pytest.mark.parametrize("tools", [[TOOL_STATUS, TOOL_REVIEWS], [TOOL_REVIEWS, TOOL_STATUS]])
def test_stub_picks_the_matching_tool_wherever_it_is_listed(gw, tools):
    gateway, _ = gw()
    reply = call(gateway, {"messages": [USER], "tools": tools}).json()
    assert reply["output"]["message"]["content"][0]["toolUse"]["name"] == "orders__get_order_status"


def test_stub_replies_with_text_when_no_tool_matches(gw):
    gateway, _ = gw()
    hello = {"role": "user", "content": [{"text": "Hello there, how are you today?"}]}
    reply = call(gateway, {"messages": [hello], "tools": [TOOL_STATUS, TOOL_REVIEWS]}).json()
    assert reply["stop_reason"] == "end_turn"
    assert reply["output"]["message"]["content"] == [{"text": StubModel().default}]


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


ARN_MESSAGE = ("User: arn:aws:sts::111122223333:assumed-role/copperline-core/i-0123456789abcdef0 is not "
               "authorized to perform: bedrock:InvokeModel")


def bedrock_gw(gw, client, **overrides):
    model = BedrockModel("us-east-1", {"fast": "model-fast"}, client=client)
    return gw(model=model, stub_model=False, aws_region="us-east-1", route_models={"fast": "model-fast"}, **overrides)


@pytest.mark.parametrize(
    "codes, status, error",
    [
        (["AccessDeniedException"], 502, "bedrock_error"),  # not retried: the model never runs, but it never will
        (["ValidationException"], 502, "bedrock_error"),
        (["ModelTimeoutException"], 504, "bedrock_timeout"),  # not retried: the model may have run
        (["ThrottlingException", "ThrottlingException"], 429, "bedrock_error"),  # retried once, then given up
        (["ServiceUnavailableException", "ServiceUnavailableException"], 502, "bedrock_error"),
    ],
)
def test_bedrock_errors_map_to_gateway_statuses(gw, bedrock, codes, status, error):
    client, stubber = bedrock
    for code in codes:
        stubber.add_client_error("converse", service_error_code=code, service_message=ARN_MESSAGE)
    gateway, admin = bedrock_gw(gw, client)
    response = call(gateway)
    assert (response.status_code, response.json()) == (status, {"error": error, "message": codes[0]})
    assert admin.get("/v1/usage").json()["calls"] == 0


def test_a_throttled_call_that_succeeds_on_retry_is_a_normal_200(gw, bedrock):
    client, stubber = bedrock
    stubber.add_client_error("converse", service_error_code="ThrottlingException")
    stubber.add_response("converse", BEDROCK_REPLY)
    gateway, admin = bedrock_gw(gw, client)
    assert call(gateway).status_code == 200
    assert admin.get("/v1/usage").json()["calls"] == 1


def test_aws_account_details_stay_in_the_log_not_the_response(gw, bedrock, capsys):
    client, stubber = bedrock
    stubber.add_client_error("converse", service_error_code="AccessDeniedException", service_message=ARN_MESSAGE)
    gateway, _ = bedrock_gw(gw, client)
    response = call(gateway)
    assert "111122223333" not in response.text and "arn:aws" not in response.text
    assert "111122223333" in capsys.readouterr().out  # the presenter can still find it in ai-gw's logs


class RaisingClient:
    """A fake bedrock-runtime client that raises the given botocore errors in turn, then succeeds."""

    def __init__(self, *errors) -> None:
        self.errors = list(errors)
        self.calls = 0

    def converse(self, **request):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return BEDROCK_REPLY


def read_timeout():
    from botocore.exceptions import ReadTimeoutError
    return ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")


def connect_timeout():
    from botocore.exceptions import ConnectTimeoutError
    return ConnectTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")


def unreachable():
    from botocore.exceptions import EndpointConnectionError
    return EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")


@pytest.mark.parametrize(
    "errors, status, error, calls",
    [
        ([read_timeout], 504, "bedrock_timeout", 1),  # never retried: the model may be running and billing
        ([connect_timeout, connect_timeout], 504, "bedrock_timeout", 2),  # never connected: one retry is safe
        ([unreachable, unreachable], 502, "bedrock_unreachable", 2),
        ([connect_timeout], 200, None, 2),  # the retry succeeds
    ],
)
def test_botocore_failures_map_to_gateway_statuses(gw, errors, status, error, calls):
    client = RaisingClient(*(make() for make in errors))
    gateway, admin = bedrock_gw(gw, client)
    response = call(gateway)
    assert response.status_code == status and client.calls == calls
    if error:
        assert response.json()["error"] == error
        assert admin.get("/v1/usage").json()["calls"] == 0


def test_a_shape_bedrock_rejects_client_side_is_400_without_echoing_it(gw):
    """botocore validates parameters before sending (ParamValidationError): the caller's fault, not an outage."""
    client = boto3.client("bedrock-runtime", region_name="us-east-1", aws_access_key_id="x",
                          aws_secret_access_key="x")
    client.meta.events.register("before-send.bedrock-runtime.Converse",
                                lambda **_: pytest.fail("validation must stop the request before it is sent"))
    gateway, _ = bedrock_gw(gw, client)
    response = call(gateway, {"messages": [{"role": "user", "content": [{"text": 5}]}]})
    assert response.status_code == 400 and response.json()["error"] == "bad_request"
    assert "value: 5" not in response.text


def test_a_multi_turn_tool_conversation_is_sent_to_bedrock_unchanged(gw, bedrock):
    """The shape agent-runtime's loop sends: user text, assistant toolUse, user toolResult; max_tokens under the cap;
    a tool with an empty description; no system prompt."""
    client, stubber = bedrock
    tool_use = {"toolUseId": "t1", "name": "orders__get_order_status", "input": {"order_id": "CO-1042"}}
    messages = [USER, {"role": "assistant", "content": [{"toolUse": tool_use}]},
                {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "shipped"}]}}]}]
    tools = [{"name": "orders__get_order_status", "description": "", "input_schema": {"type": "object"}}]
    stubber.add_response("converse", BEDROCK_REPLY, {
        "modelId": "model-fast",
        "messages": messages,
        "inferenceConfig": {"maxTokens": 400},
        "toolConfig": {"tools": [{"toolSpec": {"name": "orders__get_order_status",
                                               "description": "orders__get_order_status",
                                               "inputSchema": {"json": {"type": "object"}}}}]},
    })
    gateway, _ = bedrock_gw(gw, client)
    assert call(gateway, {"messages": messages, "tools": tools, "max_tokens": 400}).status_code == 200


def test_create_gateway_app_wires_bedrock_from_settings(gw):
    """The production path, with no injected model: route map and read timeout come from Settings; no AWS call."""
    gateway, _ = gw(stub_model=False, aws_region="us-east-1", route_models={"fast": "model-fast"},
                    bedrock_timeout_s=7.0)
    model = gateway.app.state.model
    assert isinstance(model, BedrockModel) and model.route_models == {"fast": "model-fast"}
    assert model.client.meta.config.read_timeout == 7.0
    assert model.client.meta.config.retries["total_max_attempts"] == 1  # retries are ours, not botocore's


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


# ---- process wiring (python -m ai_gw) ------------------------------------------------------------------------------


def test_every_log_line_says_ai_gw_even_after_the_admin_app_is_built(capsys):
    """Both apps live in one process; the admin app must not re-tag the gateway's logs as ai-gw-admin."""
    import json

    s = settings()
    usage = Usage(s.budgets)
    gateway = TestClient(create_gateway_app(s, usage))
    create_admin_app(s, usage)  # built second, as python -m ai_gw does
    capsys.readouterr()
    call(gateway)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    converse_lines = [line for line in lines if line["event"] == "converse"]
    assert converse_lines and {line["service"] for line in lines} == {"ai-gw"}
    assert gateway.post("/v1/converse").headers["x-copperline-origin"] == "ai-gw"  # the header is still per app


def test_python_m_ai_gw_serves_both_apps_on_one_usage_table(monkeypatch):
    """What the container runs. A second Usage for the admin app would make the cockpit show 0 calls and make
    "reset" clear a table the gateway never reads."""
    import asyncio

    from ai_gw import __main__ as main

    started = {}

    class FakeServer:
        def __init__(self, app, port):
            self.app, self.port = app, port

        async def serve(self):
            started[self.port] = self.app

    monkeypatch.setattr(main, "server", lambda app, host, port: FakeServer(app, port))
    asyncio.run(main.serve(settings(port=9001, admin_port=9002)))
    gateway, admin = TestClient(started[9001]), TestClient(started[9002])
    assert call(gateway).status_code == 200
    assert admin.get("/v1/usage").json()["calls"] == 1
    admin.post("/v1/usage/reset")
    assert started[9001].state.usage.snapshot()["calls"] == 0


def test_settings_defaults_from_env(monkeypatch):
    for name in ("AIGW_PORT", "AIGW_ADMIN_PORT", "GATEWAY_REQUIRE_KEY", "DEMO_STUB_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_API_KEY", "k")
    monkeypatch.setenv("DEMO_STUB_MODEL", "true")
    s = Settings.from_env()
    assert (s.port, s.admin_port, s.require_key) == (8090, 8190, True)
    monkeypatch.setenv("AIGW_ADMIN_PORT", "9190")
    assert Settings.from_env().admin_port == 9190
