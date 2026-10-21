"""scripts/run_scenario.py: the parts that decide what runs and what counts as a pass. Running scenarios against a real
stack is covered live (make local-scenarios on the devbox)."""

from datetime import timedelta
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
    response.elapsed = timedelta(milliseconds=5)
    row = run_scenario.front_door_row(3, response)
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


def test_dry_run_never_hides_exfil_test_url_outside_the_local_job(monkeypatch):
    """The step-7 review found S5 failing in the tenant on a missing EXFIL_TEST_URL. A dry run must not paper over it
    except where compose really sets it (the local job)."""
    import demo_env

    monkeypatch.delenv("EXFIL_TEST_URL", raising=False)
    s5 = by_id(SCENARIOS, "S5")
    tenantish = demo_env.DemoEnv(name="tenant", front_doors={}, plan_env={"TOOLS_BASE_URL": "http://t"})
    assert run_scenario.dry_run([s5], tenantish) == 1


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


def test_the_smoke_test_reports_every_hop_without_crashing(monkeypatch, capsys):
    """Every hop ok, then one hop missing and one hop blocked: each is a line, never a traceback."""
    import smoke_test

    def fake_stack(trace):
        def handler(request):
            path = request.url.path
            if path.endswith("/auth/demo-login"):
                return httpx.Response(200, json={"access_token": "t"})
            if path == "/v1/chat":
                return httpx.Response(200, json={"trace": trace})
            if path == "/admin/state" and request.url.port == 8014:
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"status": "ok"})
        return httpx.MockTransport(handler)

    real_client = httpx.Client
    good = [{"kind": k, "status": 200, "blocked_by": None} for k in ("token", "tool", "model")]
    blocked_model = good[:2] + [{"kind": "model", "status": 403, "blocked_by": "xc", "reason": "service policy"}]
    for trace, expected in ((good, 0), (blocked_model, 1), (good[:1], 1)):
        transport = fake_stack(trace)

        def client_on_fake_stack(*args, transport=transport, **kwargs):
            return real_client(transport=transport, **kwargs)

        monkeypatch.setattr(smoke_test.httpx, "Client", client_on_fake_stack)
        assert smoke_test.main(["--env", "local"]) == expected
    out = capsys.readouterr().out
    assert "FAIL  model call through llm-lb  (403 service policy)" in out
    assert "FAIL  tool call through tools-lb  (not in the trace)" in out
