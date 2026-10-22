"""copperline_common.demo_driver: the one way scenarios are driven and judged (cockpit and run_scenario.py). End-to-end
runs against the real services are in deploy/local/local-gateway/tests/; these pin the contract with fakes."""

import json

import httpx
import pytest
from copperline_common import demo_driver
from copperline_common.config import repo_root
from copperline_common.personas import Personas
from copperline_common.scenarios import by_id, load_scenarios

REPO = repo_root()
SCENARIOS = load_scenarios(REPO / "scenarios")
PERSONAS = Personas.load(REPO / "config/personas.yaml")
ENDPOINTS = demo_driver.Endpoints(
    name="local", front_doors={"shop-lb": "http://shop", "ops-lb": "http://ops", "partner-lb": "http://partner"},
    erp_admin_url="http://erp-admin", plan_env={"PARTNER_BASE_URL": "http://partner"})


def patch_client(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(demo_driver.httpx, "Client",
                        lambda *a, **kw: real(transport=httpx.MockTransport(handler),
                                              **{k: v for k, v in kw.items() if k != "transport"}))


def stack_handler(chat_answer, logins=None):
    def handler(request):
        if request.url.path == "/auth/demo-login":
            if logins is not None:
                logins.append(json.loads(request.content))
            return httpx.Response(200, json={"access_token": "t"})
        if request.url.path == "/admin/state":
            return httpx.Response(200, json={"refunds_issued": 0, "stock_writes": 0})
        return chat_answer(request)
    return handler


def test_a_chat_scenario_logs_in_through_its_front_door_with_the_salt_and_hint(monkeypatch):
    logins, chats = [], []

    def answer(request):
        chats.append((request.url.host, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"reply": "hi", "scenario": "S3a", "trace": []})
    patch_client(monkeypatch, stack_handler(answer, logins))
    outcome = demo_driver.run_one(by_id(SCENARIOS, "S3a"), ENDPOINTS, PERSONAS, "salt9")
    assert logins == [{"sub": "sam.patel", "run_salt": "salt9"}]
    assert chats[0][:2] == ("ops", "/refunds/v1/chat") and chats[0][2]["scenario_hint"] == "S3a"
    assert outcome["matched"] == "S3a" and outcome["estate_delta"] == {"refunds_issued": 0, "stock_writes": 0}


def test_run_one_never_raises(monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused", request=request)
    patch_client(monkeypatch, down)
    outcome = demo_driver.run_one(by_id(SCENARIOS, "S0"), ENDPOINTS, PERSONAS, "s")
    assert outcome["trace"] == [] and outcome["error"].startswith("unreachable")


def test_a_job_scenario_without_a_runner_is_an_error():
    outcome = demo_driver.run_one(by_id(SCENARIOS, "S5"), ENDPOINTS, PERSONAS, "s", run_job=None)
    assert "no way to start the batch job" in outcome["error"]


def test_a_job_scenario_reads_trace_lines_from_the_runner(monkeypatch):
    patch_client(monkeypatch, stack_handler(lambda r: httpx.Response(404)))
    lines = "noise\n" + "\n".join("TRACE " + json.dumps({"i": i, "kind": "token"}) for i in range(2))
    outcome = demo_driver.run_one(by_id(SCENARIOS, "S5"), ENDPOINTS, PERSONAS, "s",
                                  run_job=lambda name: (0, lines, ""))
    assert [s["i"] for s in outcome["trace"]] == [0, 1] and outcome["error"] is None


def test_the_partner_secret_never_lands_in_the_process_environment(monkeypatch):
    """The cockpit runs S6 many times in one process: the secret goes in the plan's env only."""
    import os

    monkeypatch.delenv("GRANITE_SUPPLY_SECRET", raising=False)
    endpoints = demo_driver.Endpoints(**{**ENDPOINTS.__dict__, "secrets": {"GRANITE_SUPPLY_SECRET": "sekret"}})
    seen = []

    async def fake_client_credentials(http, url, client_id, secret, audience):
        seen.append(secret)
        raise RuntimeError("stop here")

    import copperline_common.plan_executor as executor
    monkeypatch.setattr(executor, "client_credentials", fake_client_credentials)
    demo_driver.run_one(by_id(SCENARIOS, "S6"), endpoints, PERSONAS, "s")
    assert seen == ["sekret"] and "GRANITE_SUPPLY_SECRET" not in os.environ


def test_a_partner_scenario_without_its_secret_is_an_error(monkeypatch):
    monkeypatch.delenv("GRANITE_SUPPLY_SECRET", raising=False)
    outcome = demo_driver.run_one(by_id(SCENARIOS, "S6"), ENDPOINTS, PERSONAS, "s")
    assert "GRANITE_SUPPLY_SECRET" in outcome["error"]


@pytest.mark.parametrize("outcome, verdict", [
    ({"trace": [], "error": "x"}, "error"),
    ({"trace": [{"i": 2, "kind": "tool", "status": 403, "blocked_by": "xc"}], "error": "job exited 1"}, "error"),
    ({"trace": [{"i": 2, "kind": "tool", "status": 403, "blocked_by": "xc"}], "matched": None}, "error"),
])
def test_judge_never_passes_an_error(outcome, verdict):
    outcome.setdefault("estate_delta", {"refunds_issued": 0, "stock_writes": 0})
    assert demo_driver.judge(by_id(SCENARIOS, "S1"), "local", outcome)["verdict"] == verdict


def test_judge_reports_no_expectations_for_s7_locally():
    assert demo_driver.judge(by_id(SCENARIOS, "S7"), "local", {"trace": []})["verdict"] == "no expectations"
