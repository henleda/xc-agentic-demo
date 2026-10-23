"""The cockpit backend against the real services in-process: every scenario button, the estate panel, reset and chat.
The cockpit drives scenarios through copperline_common.demo_driver, like scripts/run_scenario.py, so each button
must give the verdict the acceptance check gives (pass for every scenario with local expectations).
"""

from pathlib import Path

import pytest
from cockpit.app import create_app
from copperline_common.demo_driver import Endpoints
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[4]
@pytest.fixture
def cockpit(cockpit_settings, in_process_job):
    settings = cockpit_settings(run_salt="t1", console_links={"S1": "https://console.example/s1"})
    with TestClient(create_app(settings, job_runner=in_process_job)) as client:
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


def test_a_scenario_against_a_stack_that_is_down_is_an_error_not_a_crash(cockpit_settings, stack):
    """The stack's front door and ERP admin really refuse connections (as stopped containers do): the button answers
    with an ERROR verdict that says the hop is unreachable, and the estate panel shows the ERP as missing."""
    del stack.mesh.apps["shop-lb"], stack.mesh.apps["erp-mock-admin"]
    with TestClient(create_app(cockpit_settings())) as client:
        result = client.post("/api/scenarios/S0/run").json()
        assert result["verdict"] == "error" and result["error"].startswith("unreachable"), result["error"]
        assert client.get("/api/estate").json()["erp"] is None


def test_a_container_running_another_plan_is_an_error_on_screen_too(cockpit_settings, monkeypatch):
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
    with TestClient(create_app(cockpit_settings())) as client:
        result = client.post("/api/scenarios/S3b/run").json()
    assert result["verdict"] == "error" and "make local-up" in result["error"]


def test_the_estate_panel_answers_while_a_scenario_is_running(cockpit_settings):
    """The page polls every 2 s; a 60 s scenario must not freeze the counters on screen."""
    import threading

    started, release = threading.Event(), threading.Event()

    def slow_job(name):
        started.set()
        release.wait(10)
        return 0, "", ""

    with TestClient(create_app(cockpit_settings(), job_runner=slow_job)) as client:
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



# ---- step-10 review: one thing at a time, chat input, reload, settings ---------------------------------------------


def test_a_second_click_while_a_run_is_busy_is_refused_not_queued(cockpit_settings):
    """A queued click would still run after the presenter moved on (a second refund, a late reset)."""
    import threading

    started, release, calls = threading.Event(), threading.Event(), []

    def slow_job(name):
        calls.append(name)
        started.set()
        release.wait(10)
        return 0, "", ""

    with TestClient(create_app(cockpit_settings(), job_runner=slow_job)) as client:
        first = threading.Thread(target=lambda: client.post("/api/scenarios/S5/run"))
        first.start()
        assert started.wait(10)
        again = client.post("/api/scenarios/S5/run")
        reset = client.post("/api/reset")
        state = client.get("/api/state").json()
        release.set()
        first.join(10)
    assert again.status_code == 409 and again.json()["running"]["what"] == "S5"
    assert reset.status_code == 409
    assert state["running"]["what"] == "S5"  # a reloaded page can show what is running
    assert calls == ["insights-nightly"]  # the job ran once: the refused click never ran later


@pytest.mark.parametrize("body", [b"[1]", b'"text"', b"{", b"", b'{"sub": "alex.rivera", "messages": [1]}',
                                  b'{"sub": "alex.rivera", "messages": [{"role": "user", "content": "hi"}], '
                                  b'"scenario_hint": 5}'])
def test_bad_chat_bodies_are_400_not_500(cockpit, body):
    response = cockpit.post("/api/chat", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json()["error"] == "bad_request"


def test_scenario_edits_reach_the_buttons_without_a_restart(cockpit_settings, tmp_path):
    import os
    import shutil

    scenarios = tmp_path / "scenarios"
    shutil.copytree(REPO / "scenarios", scenarios)
    with TestClient(create_app(cockpit_settings(scenarios_dir=scenarios))) as client:
        before = next(s for s in client.get("/api/state").json()["scenarios"] if s["id"] == "S0")["message"]
        path = scenarios / "S0-baseline.yaml"
        path.write_text(path.read_text().replace(before, "Edited message for CO-1042"))
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 10**9))
        after = next(s for s in client.get("/api/state").json()["scenarios"] if s["id"] == "S0")["message"]
    assert after == "Edited message for CO-1042"


def test_the_cockpit_refuses_to_start_with_no_scenarios(cockpit_settings, tmp_path):
    with pytest.raises(RuntimeError, match="no scenarios"):
        create_app(cockpit_settings(scenarios_dir=tmp_path))


def test_the_acting_line_names_the_human_and_role_for_every_agent_scenario(cockpit):
    acts = {s["id"]: s["acts_for"] for s in cockpit.get("/api/state").json()["scenarios"]}
    assert acts["S3a"] == "refunds agent, acting for sam.patel (care_agent)"
    assert acts["S3b"] == "refunds agent, acting for morgan.chen (care_supervisor)"
    assert "no human behind it" in acts["S5"] and "third-party" in acts["S6"]


def test_the_runner_and_the_cockpit_judge_a_stale_s5_alike(cockpit_settings, monkeypatch):
    """S5's plan has ${EXFIL_TEST_URL}; both must still run the stale-plan check on it (the review found the runner
    silently skipped it)."""
    import sys

    from copperline_common import demo_driver
    from copperline_common.scenarios import by_id, load_scenarios

    sys.path.insert(0, str(REPO / "scripts"))
    import run_scenario

    monkeypatch.delenv("EXFIL_TEST_URL", raising=False)
    s5 = by_id(load_scenarios(REPO / "scenarios"), "S5")
    stale = {"trace": [{"i": 0, "kind": "token", "scripted": True, "status": 200, "blocked_by": None},
                       {"i": 1, "kind": "token", "scripted": True, "status": 200, "blocked_by": None},
                       {"i": 2, "kind": "tool", "tool": "orders.get_order_status", "scripted": True, "status": 403,
                        "blocked_by": "xc"}]}
    # The runner end to end through main() (only run_one stubbed), so its own choice of judge env is what is tested.
    monkeypatch.setattr(run_scenario, "run_one", lambda scenario, env, personas, run_salt: dict(stale))
    monkeypatch.setattr(run_scenario, "write_results", lambda *args: None)
    runner = {"verdict": "error" if run_scenario.main(["S5", "--env", "local"]) == 1 else "pass"}
    cockpit = demo_driver.judge(s5, "local", dict(stale), demo_driver.judge_env(cockpit_settings().endpoints))
    assert runner["verdict"] == cockpit["verdict"] == "error"


def test_endpoints_never_print_their_secrets(local_ports):

    endpoints = Endpoints(name="local", front_doors={}, secrets={"GRANITE_SUPPLY_SECRET": "do-not-print-me"})
    assert "do-not-print-me" not in repr(endpoints) and "do-not-print-me" not in str(endpoints)
