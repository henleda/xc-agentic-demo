"""scripts/run_scenario.py: the parts that decide what runs and what counts as a pass. Running scenarios against a real
stack is covered live (make local-scenarios on the devbox)."""

from pathlib import Path

import httpx
import pytest
import run_scenario
from copperline_common.scenarios import by_id, load_scenarios

REPO = Path(__file__).resolve().parents[2]
SCENARIOS = load_scenarios(REPO / "scenarios")


def test_s4_and_s6_run_last_and_nothing_is_dropped():
    order = [s.id for s in run_scenario.ordered(SCENARIOS, None)]
    assert order[-2:] == ["S4", "S6"]
    assert sorted(order) == sorted(s.id for s in SCENARIOS)


def test_a_subset_keeps_s4_and_s6_last():
    assert [s.id for s in run_scenario.ordered(SCENARIOS, ["S6", "S0", "S4", "S1"])] == ["S0", "S1", "S4", "S6"]


def test_an_unknown_id_is_refused():
    with pytest.raises(SystemExit, match="S99"):
        run_scenario.ordered(SCENARIOS, ["S0", "S99"])


def test_no_expectations_is_never_a_pass():
    """S7 has an empty local block: it must be reported as such, and never counted as passed."""
    result = run_scenario.check(by_id(SCENARIOS, "S7"), "local", {"trace": []})
    assert result["verdict"] == "no expectations"


def test_a_failed_check_is_a_fail_with_the_failing_check_listed():
    trace = [{"n": 1, "i": 2, "kind": "tool", "status": 200, "blocked_by": None, "is_error": False}]
    result = run_scenario.check(by_id(SCENARIOS, "S1"), "local", {"trace": trace, "estate_delta": {
        "refunds_issued": 0, "stock_writes": 0}})
    assert result["verdict"] == "fail"
    assert any(not c["ok"] and "i=2" in c["description"] for c in result["checks"])


def test_a_passing_trace_is_a_pass():
    trace = [{"n": 1, "i": 2, "kind": "tool", "status": 403, "blocked_by": "xc", "is_error": None}]
    result = run_scenario.check(by_id(SCENARIOS, "S1"), "local", {"trace": trace, "estate_delta": {
        "refunds_issued": 0, "stock_writes": 0}})
    assert result["verdict"] == "pass"


def test_an_error_with_no_trace_is_an_error_not_a_fail():
    result = run_scenario.check(by_id(SCENARIOS, "S1"), "local", {"trace": [], "error": "login failed"})
    assert result["verdict"] == "error" and result["error"] == "login failed"


def test_estate_not_observed_fails_a_scenario_that_expects_it():
    """S3b expects refunds_issued: 1. If the ERP admin port could not be read, that is a failure, not a pass."""
    trace = [{"n": 1, "i": 1, "kind": "tool", "status": 200, "blocked_by": None, "is_error": False}]
    result = run_scenario.check(by_id(SCENARIOS, "S3b"), "local", {"trace": trace, "estate_delta": None})
    assert result["verdict"] == "fail"


@pytest.mark.parametrize("status, headers, blocked_by", [
    (403, {}, "xc"),                                        # no origin header: the fabric (or its stand-in)
    (200, {"x-copperline-origin": "shopper-agent"}, None),
    (401, {"x-copperline-origin": "shopper-agent"}, "server"),
])
def test_front_door_rows_are_classified_like_trace_steps(status, headers, blocked_by):
    response = httpx.Response(status, headers=headers, text="{}", request=httpx.Request("POST", "http://x"))
    row = run_scenario.front_door_row(3, response, started=0.0)
    assert (row["kind"], row["attempt"], row["status"], row["blocked_by"]) == ("front_door", 3, status, blocked_by)


def test_dry_run_reports_an_unresolved_variable(capsys, monkeypatch):
    """S6 needs ${PARTNER_BASE_URL}. Without it the dry run must fail, so a missing variable is caught before a show."""
    import demo_env

    monkeypatch.delenv("PARTNER_BASE_URL", raising=False)
    env = demo_env.local()
    env.plan_env.pop("PARTNER_BASE_URL")
    assert run_scenario.dry_run([by_id(SCENARIOS, "S6")], env) == 1
    assert "PARTNER_BASE_URL" in capsys.readouterr().out


def test_dry_run_of_every_scenario_passes_locally():
    import demo_env

    assert run_scenario.dry_run(run_scenario.ordered(SCENARIOS, None), demo_env.local()) == 0


def test_tenant_dry_run_checks_s5_against_what_the_pod_will_get(monkeypatch, tmp_path):
    """The step-7 review found S5 failing in the tenant on a missing EXFIL_TEST_URL. The dry run reads the job's env
    from the manifests (runtime-env ConfigMap + EXFIL_TEST_URL from .env), so it fails exactly when the pod would."""
    import demo_env

    monkeypatch.setattr(demo_env, "ROOT", REPO)
    monkeypatch.setenv("DEMO_DOMAIN", "example.test")
    monkeypatch.delenv("EXFIL_TEST_URL", raising=False)
    monkeypatch.setattr(demo_env, "read_env_file", lambda path: {})  # an .env without EXFIL_TEST_URL
    s5 = by_id(SCENARIOS, "S5")
    assert run_scenario.dry_run([s5], demo_env.tenant()) == 1
    monkeypatch.setattr(demo_env, "read_env_file", lambda path: {"EXFIL_TEST_URL": "https://bin.example.test/x"})
    assert run_scenario.dry_run([s5], demo_env.tenant()) == 0


def test_local_dry_run_reads_the_jobs_env_from_compose(monkeypatch):
    import demo_env

    monkeypatch.delenv("EXFIL_TEST_URL", raising=False)
    env = demo_env.job_env(demo_env.local())
    assert env["EXFIL_TEST_URL"] == "https://exfil.invalid/rows" and env["TOOLS_BASE_URL"].startswith("http://")


def test_the_cli_needs_ids_or_all():
    with pytest.raises(SystemExit):
        run_scenario.main(["--env", "local"])
    with pytest.raises(SystemExit):
        run_scenario.main(["S0", "--all", "--env", "local"])


def test_scenario_ids_are_unique():
    """by_id and --all would silently pick one of two scenarios with the same id."""
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))


def test_every_scenario_is_drivable():
    """Each persona is a login, a job or a partner, and each agent scenario names an agent with a chat path."""
    import demo_env
    from copperline_common.personas import Personas

    personas = Personas.load(REPO / "config/personas.yaml")
    for scenario in SCENARIOS:
        if scenario.persona.startswith(("job:", "partner:")):
            continue
        assert personas.get(scenario.persona), scenario.id
        assert scenario.agent in demo_env.CHAT_PATH, scenario.id


# ---- step-9 review: errors are data, verdicts, main() --------------------------------------------------------------


import json  # noqa: E402
import subprocess  # noqa: E402

import demo_env  # noqa: E402
from copperline_common import demo_driver  # noqa: E402
from copperline_common.personas import Personas  # noqa: E402

PERSONAS = Personas.load(REPO / "config/personas.yaml")


def fake_client(handler):
    """Patch httpx.Client inside run_scenario so every request goes to `handler(request)`."""
    real = httpx.Client

    def make(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)
    return make


@pytest.mark.parametrize("error, says", [
    (httpx.ConnectError("refused"), "unreachable"),
    (httpx.ReadTimeout("slow"), "timed out"),
])
def test_a_hop_that_is_down_is_this_scenarios_error_not_a_crash(monkeypatch, error, says):
    def handler(request):
        error.request = request
        raise error
    monkeypatch.setattr(demo_driver.httpx, "Client", fake_client(handler))
    outcome = run_scenario.run_one(by_id(SCENARIOS, "S0"), demo_env.local(), PERSONAS, "salt1")
    assert outcome["trace"] == [] and says in outcome["error"]


def test_a_body_that_is_not_json_is_an_error_not_a_crash(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/auth/demo-login"):
            return httpx.Response(200, json={"access_token": "t"})
        if request.url.path == "/admin/state":
            return httpx.Response(200, json={"refunds_issued": 0, "stock_writes": 0})
        return httpx.Response(200, text="<html>not json</html>")
    monkeypatch.setattr(demo_driver.httpx, "Client", fake_client(handler))
    outcome = run_scenario.run_one(by_id(SCENARIOS, "S0"), demo_env.local(), PERSONAS, "salt1")
    assert "JSONDecodeError" in outcome["error"]


@pytest.mark.parametrize("body, status", [({"detail": "x"}, 404), ({"refunds_issued": "a"}, 200), ([], 200)])
def test_estate_is_none_unless_the_counters_really_came_back(body, status):
    env = demo_env.local()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))
    assert run_scenario.estate(client, env) is None


def test_a_job_that_exits_non_zero_after_some_steps_is_an_error_not_a_pass(monkeypatch):
    """The step-9 review: S5's local expect only asserts i=2, so a crash after it used to be reported PASS."""
    lines = [json.dumps({"i": i, "kind": k, "status": s, "blocked_by": b, "scripted": True, "tool": t})
             for i, k, s, b, t in ((0, "token", 200, None, None), (1, "token", 200, None, None),
                                   (2, "tool", 403, "xc", "orders.list_customer_orders"))]
    done = subprocess.CompletedProcess([], 137, stdout="\n".join("TRACE " + line for line in lines), stderr="Killed")
    monkeypatch.setattr(run_scenario.subprocess, "run", lambda *a, **kw: done)
    monkeypatch.setattr(demo_driver.httpx, "Client", fake_client(
        lambda r: httpx.Response(200, json={"refunds_issued": 0, "stock_writes": 0})))
    s5 = by_id(SCENARIOS, "S5")
    result = run_scenario.check(s5, "local", run_scenario.run_one(s5, demo_env.local(), PERSONAS, "x"))
    assert result["verdict"] == "error" and "exited 137" in result["error"]


def test_a_job_that_hangs_is_an_error(monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))
    monkeypatch.setattr(run_scenario.subprocess, "run", hang)
    monkeypatch.setattr(demo_driver.httpx, "Client", fake_client(
        lambda r: httpx.Response(200, json={"refunds_issued": 0, "stock_writes": 0})))
    outcome = run_scenario.run_one(by_id(SCENARIOS, "S5"), demo_env.local(), PERSONAS, "x")
    assert "did not finish" in outcome["error"]


def test_an_agent_that_ran_another_scenario_is_an_error_even_if_expect_happens_to_match():
    s1 = by_id(SCENARIOS, "S1")
    trace = [{"n": 1, "i": 2, "kind": "tool", "status": 403, "blocked_by": "xc", "is_error": None}]
    result = run_scenario.check(s1, "local", {"trace": trace, "matched": None,
                                              "estate_delta": {"refunds_issued": 0, "stock_writes": 0}})
    assert result["verdict"] == "error" and "normal loop" in result["error"]


def test_a_stale_plan_in_the_container_is_an_error():
    """The agent image bakes scenarios/ in; a plan edited on disk but not rebuilt must not pass."""
    s3b = by_id(SCENARIOS, "S3b")
    trace = [{"i": 0, "kind": "tool", "tool": "orders.get_order_details", "scripted": True, "status": 200,
              "blocked_by": None, "is_error": False},
             {"i": 1, "kind": "tool", "tool": "refunds.get_refund_status", "scripted": True, "status": 200,
              "blocked_by": None, "is_error": False}]
    env = demo_env.local().plan_env
    result = run_scenario.check(s3b, "local", {"trace": trace, "matched": "S3b",
                                               "estate_delta": {"refunds_issued": 1, "stock_writes": 0}}, env)
    assert result["verdict"] == "error" and "make local-up" in result["error"]


def test_s7_sends_its_message_repeat_message_times_and_records_each_answer(monkeypatch):
    chats = []

    def handler(request):
        if request.url.path.endswith("/auth/demo-login"):
            return httpx.Response(200, json={"access_token": "t"})
        if request.url.path == "/admin/state":
            return httpx.Response(200, json={"refunds_issued": 0, "stock_writes": 0})
        chats.append(json.loads(request.content))
        return httpx.Response(403, json={"error": "blocked"})  # no origin header: the fabric
    monkeypatch.setattr(demo_driver.httpx, "Client", fake_client(handler))
    s7 = by_id(SCENARIOS, "S7")
    outcome = run_scenario.run_one(s7, demo_env.local(), PERSONAS, "salt1")
    assert len(chats) == s7.repeat_message and all(c["scenario_hint"] == "S7" for c in chats)
    assert [(r["attempt"], r["status"], r["blocked_by"]) for r in outcome["trace"]] == [
        (n, 403, "xc") for n in range(1, s7.repeat_message + 1)]


def test_mallory_logs_in_with_the_runs_salt(monkeypatch):
    logins = []

    def handler(request):
        if request.url.path.endswith("/auth/demo-login"):
            logins.append(json.loads(request.content))
            return httpx.Response(500)
        return httpx.Response(200, json={"refunds_issued": 0, "stock_writes": 0})
    monkeypatch.setattr(demo_driver.httpx, "Client", fake_client(handler))
    run_scenario.run_one(by_id(SCENARIOS, "S1"), demo_env.local(), PERSONAS, "abc123")
    assert logins == [{"sub": "mallory.x", "run_salt": "abc123"}]


# ---- main(): exit codes, salts, waits, results -----------------------------------------------------------------


@pytest.fixture
def stubbed_main(monkeypatch, tmp_path):
    """main() with run_one replaced: returns (calls, set_outcome). Results go to tmp_path; sleeps are recorded."""
    calls, outcomes, sleeps = [], {}, []

    def fake_run_one(scenario, env, personas, run_salt):
        calls.append((scenario.id, run_salt))
        return outcomes.get(scenario.id, {"trace": [], "error": "not stubbed"})

    monkeypatch.setattr(run_scenario, "run_one", fake_run_one)
    monkeypatch.setattr(run_scenario, "ROOT", tmp_path)
    (tmp_path / "scenarios").symlink_to(REPO / "scenarios")
    (tmp_path / "config").symlink_to(REPO / "config")
    monkeypatch.setattr(run_scenario.time, "sleep", sleeps.append)
    monkeypatch.delenv("RUN_SALT", raising=False)
    return calls, outcomes, sleeps, tmp_path


S1_PASS = {"trace": [  # S1's whole plan as the agent returns it: reviews read, the hijacked model turn, refund denied
    {"n": 3, "i": 0, "kind": "tool", "tool": "catalog.get_reviews", "status": 200, "blocked_by": None,
     "is_error": False, "scripted": True},
    {"n": 4, "i": 1, "kind": "model", "tool": None, "status": 200, "blocked_by": None, "scripted": True},
    {"n": 5, "i": 2, "kind": "tool", "tool": "refunds.issue_refund", "status": 403, "blocked_by": "xc",
     "is_error": None, "scripted": True}],
    "matched": "S1", "estate_delta": {"refunds_issued": 0, "stock_writes": 0}}


def test_main_exits_0_only_when_every_checked_scenario_passed(stubbed_main):
    calls, outcomes, _, _ = stubbed_main
    outcomes["S1"] = S1_PASS
    assert run_scenario.main(["S1", "--env", "local"]) == 0
    refund_allowed = {**S1_PASS["trace"][2], "status": 200, "blocked_by": None, "is_error": False}
    outcomes["S1"] = {**S1_PASS, "trace": S1_PASS["trace"][:2] + [refund_allowed]}
    assert run_scenario.main(["S1", "--env", "local"]) == 1


def test_main_exits_1_when_nothing_was_checked(stubbed_main):
    """S7 alone has no local expectations: "0/0 passed" must not be a green run."""
    assert run_scenario.main(["S7", "--env", "local"]) == 1


def test_main_refuses_runs_below_1():
    with pytest.raises(SystemExit):
        run_scenario.main(["S1", "--env", "local", "--runs", "0"])


def test_each_run_gets_a_fresh_salt_and_the_wait_follows_s4_and_s6(stubbed_main):
    calls, outcomes, sleeps, _ = stubbed_main
    outcomes["S1"] = S1_PASS
    run_scenario.main(["S1", "S4", "--env", "local", "--runs", "3"])
    salts = [salt for scenario_id, salt in calls if scenario_id == "S1"]
    assert len(salts) == 3 and len(set(salts)) == 3
    assert len(sleeps) == 2 and all(0 < s <= run_scenario.RATE_LIMIT_WAIT_S for s in sleeps)


def test_no_wait_when_no_rate_limited_scenario_ran(stubbed_main):
    _, outcomes, sleeps, _ = stubbed_main
    outcomes["S1"] = S1_PASS
    run_scenario.main(["S1", "--env", "local", "--runs", "2"])
    assert sleeps == []


def test_results_are_written_after_every_scenario(stubbed_main, monkeypatch):
    """An interrupt part-way keeps what already ran."""
    calls, outcomes, _, tmp_path = stubbed_main
    outcomes["S1"] = S1_PASS

    def interrupt_on_s2(scenario, env, personas, run_salt):
        if scenario.id == "S2":
            raise KeyboardInterrupt
        return outcomes["S1"]
    monkeypatch.setattr(run_scenario, "run_one", interrupt_on_s2)
    with pytest.raises(KeyboardInterrupt):
        run_scenario.main(["S1", "S2", "--env", "local"])
    files = list((tmp_path / "results").glob("scenarios-*.json"))
    saved = json.loads(files[0].read_text())["results"]
    assert len(files) == 1 and [r["scenario"] for r in saved] == ["S1"] and saved[0]["verdict"] == "pass"
