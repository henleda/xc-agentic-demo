"""The cockpit backend against the real services in-process: every scenario button, the estate panel, reset and chat.
The cockpit drives scenarios through copperline_common.demo_driver, like scripts/run_scenario.py, so each button
must give the verdict the acceptance check gives (pass for every scenario with local expectations).
"""

from pathlib import Path

import pytest
from cockpit.app import create_app
from cockpit.settings import Settings
from copperline_common.demo_driver import Endpoints
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
LOCAL = Endpoints(
    name="local",
    front_doors={"shop-lb": "http://127.0.0.1:8001", "ops-lb": "http://127.0.0.1:8002",
                 "partner-lb": "http://127.0.0.1:8003"},
    tools_base_url="http://127.0.0.1:8011", llm_base_url="http://127.0.0.1:8012", idp_base_url="http://127.0.0.1:8013",
    erp_admin_url="http://127.0.0.1:8181", aigw_admin_url="http://127.0.0.1:8190",
    plan_env={"PARTNER_BASE_URL": "http://127.0.0.1:8003"})


def batch_job_in_process(stack):
    """S5 without docker: run the real batch mode in this process against the stack, return its TRACE output."""
    import asyncio
    import contextlib
    import io
    import os

    from agent_runtime.batch import run_batch
    from agent_runtime.settings import AgentConfig
    from agent_runtime.settings import Settings as AgentSettings

    def run_job(name: str) -> tuple[int, str, str]:
        settings = AgentSettings(agent=AgentConfig.load(REPO / f"config/agents/{name}.yaml"), client_secret=None,
                                 tools_base_url="http://tools.agentic-demo", llm_base_url="http://llm.agentic-demo",
                                 idp_base_url="http://idp.agentic-demo", config_dir=REPO / "config",
                                 scenarios_dir=REPO / "scenarios", mode="batch")
        out = io.StringIO()
        os.environ["EXFIL_TEST_URL"] = "https://exfil.invalid/rows"
        with contextlib.redirect_stdout(out):
            code = asyncio.run(run_batch(settings, http=stack.http))
        return code, out.getvalue(), ""
    return run_job


@pytest.fixture
def cockpit(stack, local_ports):
    endpoints = Endpoints(**{**LOCAL.__dict__, "secrets": local_ports})
    settings = Settings(env="local", endpoints=endpoints, scenarios_dir=REPO / "scenarios",
                        personas_path=REPO / "config/personas.yaml", run_salt="t1",
                        console_links={"S1": "https://console.example/s1"})
    with TestClient(create_app(settings, job_runner=batch_job_in_process(stack))) as client:
        yield client


def test_state_lists_every_scenario_and_persona(cockpit):
    state = cockpit.get("/api/state").json()
    assert [s["id"] for s in state["scenarios"]] == ["S0", "S1", "S1b", "S2", "S3a", "S3b", "S4", "S5", "S6", "S7"]
    assert {p["sub"] for p in state["personas"]} >= {"alex.rivera", "mallory.x", "jordan.lee", "sam.patel"}
    assert next(s for s in state["scenarios"] if s["id"] == "S1")["console_link"] == "https://console.example/s1"
    assert state["run_salt"] == "t1"


@pytest.mark.parametrize("scenario_id", ["S0", "S1", "S1b", "S2", "S3a", "S3b", "S4", "S5", "S6"])
def test_every_scenario_button_passes_like_the_acceptance_check(cockpit, scenario_id):
    result = cockpit.post(f"/api/scenarios/{scenario_id}/run").json()
    failed = [c for c in result["checks"] if not c["ok"]]
    assert result["verdict"] == "pass", (result["error"], failed)
    assert result["trace"]


def test_s7_button_sends_its_message_repeat_message_times(cockpit):
    result = cockpit.post("/api/scenarios/S7/run").json()
    assert result["verdict"] == "no expectations"  # locally: no WAF in front, so nothing to assert
    assert [row["attempt"] for row in result["trace"]] == list(range(1, 9))


def test_the_estate_panel_shows_the_refund_s3b_issues_and_reset_clears_it(cockpit):
    before = cockpit.get("/api/estate").json()["erp"]["refunds_issued"]
    cockpit.post("/api/scenarios/S3b/run")
    estate = cockpit.get("/api/estate").json()
    assert estate["erp"]["refunds_issued"] == before + 1
    assert estate["ai_gateway"]["mode"] == "stub" and estate["ai_gateway"]["calls"] >= 1
    reset = cockpit.post("/api/reset").json()
    assert reset["erp"] and reset["ai_gateway"] and reset["run_salt"] != "t1"
    after = cockpit.get("/api/estate").json()
    assert after["erp"]["refunds_issued"] == 0 and after["ai_gateway"]["calls"] == 0


def test_after_reset_mallory_logs_in_with_the_new_salt(cockpit):
    salt = cockpit.post("/api/reset").json()["run_salt"]
    result = cockpit.post("/api/scenarios/S1/run").json()
    token_identity = next(s for s in result["trace"] if s["kind"] == "token")["identity"]
    assert token_identity["obo_sub"] == f"mallory.x-{salt}" and result["verdict"] == "pass"


def test_free_chat_as_a_persona(cockpit):
    question = [{"role": "user", "content": "What is the status of my order?"}]
    result = cockpit.post("/api/chat", json={"sub": "alex.rivera", "messages": question}).json()
    assert result["reply"] and result["scenario"] is None and result["trace"]


def test_bad_requests(cockpit):
    assert cockpit.post("/api/scenarios/S99/run").status_code == 404
    assert cockpit.post("/api/chat", json={"sub": "nobody", "messages": []}).status_code == 400


def test_the_page_is_served_and_has_no_external_dependencies(cockpit):
    """Offline on stage: no CDN, no external fonts or scripts."""
    page = cockpit.get("/")
    assert page.status_code == 200 and "Copperline cockpit" in page.text
    assert "http://" not in page.text and "https://" not in page.text
    assert "#e4002b" in page.text.lower()  # the deck's F5 red for XC blocks


def test_a_scenario_against_a_stack_that_is_down_is_an_error_not_a_crash(stack, local_ports):
    """Endpoints that point nowhere: every button answers with an ERROR verdict and the reason."""
    nowhere = Endpoints(name="local", front_doors={"shop-lb": "http://127.0.0.1:9", "ops-lb": "http://127.0.0.1:9",
                                                   "partner-lb": "http://127.0.0.1:9"})
    settings = Settings(env="local", endpoints=nowhere, scenarios_dir=REPO / "scenarios",
                        personas_path=REPO / "config/personas.yaml")
    with TestClient(create_app(settings, job_runner=None)) as client:
        result = client.post("/api/scenarios/S0/run").json()
        assert result["verdict"] == "error" and result["error"]
        assert client.get("/api/estate").json()["erp"] is None


def test_a_container_running_another_plan_is_an_error_on_screen_too(stack, local_ports, monkeypatch):
    """Judged like the runner: a stale image after a plan edit must never show as a green PASS to the presenter."""
    from copperline_common import demo_driver

    real_run_one = demo_driver.run_one

    def stale(scenario, *args, **kwargs):
        outcome = real_run_one(scenario, *args, **kwargs)
        for step in outcome["trace"]:
            if step.get("tool") == "refunds.issue_refund":
                step["tool"] = "refunds.get_refund_status"  # what an older plan would have called
        return outcome

    monkeypatch.setattr(demo_driver, "run_one", stale)
    endpoints = Endpoints(**{**LOCAL.__dict__, "secrets": local_ports})
    settings = Settings(env="local", endpoints=endpoints, scenarios_dir=REPO / "scenarios",
                        personas_path=REPO / "config/personas.yaml")
    with TestClient(create_app(settings)) as client:
        result = client.post("/api/scenarios/S3b/run").json()
    assert result["verdict"] == "error" and "make local-up" in result["error"]


def test_the_estate_panel_answers_while_a_scenario_is_running(stack, local_ports):
    """The page polls every 2 s; a 60 s scenario must not freeze the counters on screen."""
    import threading

    started, release = threading.Event(), threading.Event()

    def slow_job(name):
        started.set()
        release.wait(10)
        return 0, "", ""

    endpoints = Endpoints(**{**LOCAL.__dict__, "secrets": local_ports})
    settings = Settings(env="local", endpoints=endpoints, scenarios_dir=REPO / "scenarios",
                        personas_path=REPO / "config/personas.yaml")
    with TestClient(create_app(settings, job_runner=slow_job)) as client:
        runner = threading.Thread(target=lambda: client.post("/api/scenarios/S5/run"))
        runner.start()
        assert started.wait(10)
        answered = threading.Event()
        poller = threading.Thread(target=lambda: (client.get("/api/estate"), answered.set()))
        poller.start()
        # The poll must answer while S5 is still holding the run lock, i.e. before we release it.
        answered_during_run = answered.wait(3)
        release.set()
        runner.join(10)
        poller.join(10)
    assert answered_during_run, "the estate poll waited for the running scenario"
