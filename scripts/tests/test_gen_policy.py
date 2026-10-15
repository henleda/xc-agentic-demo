"""gen_policy findings. Each test builds a tiny matrix + agent dir so one finding is exercised at a time."""
from pathlib import Path

import gen_policy
import yaml


def _matrix(**agent_overrides) -> dict:
    agent = {
        "agent_role": "shopper",
        "allowed_user_roles": ["customer"],
        "model_routes": ["fast"],
        "tools": ["catalog.get_product", {"tool": "refunds.issue_refund", "require_obo_roles": ["care_supervisor"]}],
    }
    agent.update(agent_overrides)
    return {
        "tool_servers": {"catalog": {"path": "/mcp/catalog"}, "refunds": {"path": "/mcp/refunds"}},
        "tools": {"catalog.get_product": {}, "refunds.issue_refund": {}},
        "agents": {"shopper": agent},
    }


def _agent_dir(tmp_path: Path, **cfg_overrides) -> Path:
    cfg = {
        "agent_id": "shopper",
        "agent_role": "shopper",
        "model_route": "fast",
        "declared_tools": ["catalog.get_product", "refunds.issue_refund"],
    }
    cfg.update(cfg_overrides)
    (tmp_path / "shopper.yaml").write_text(yaml.safe_dump(cfg))
    return tmp_path


def _codes(findings: list[str]) -> list[str]:
    return [f.split()[0] for f in findings]


def test_clean_config_has_no_findings(tmp_path):
    errors, warnings = gen_policy.check_agents(_matrix(), _agent_dir(tmp_path))
    assert errors == [] and warnings == []


def test_overdeclared_is_error_unless_flagged(tmp_path):
    m = _matrix(tools=["catalog.get_product"])
    errors, _ = gen_policy.check_agents(m, _agent_dir(tmp_path))
    assert _codes(errors) == ["OVERDECLARED"]

    errors, warnings = gen_policy.check_agents(m, _agent_dir(tmp_path, demo_overdeclare=True))
    assert errors == [] and _codes(warnings) == ["OVERDECLARED"]


def test_undeclared_is_warning(tmp_path):
    errors, warnings = gen_policy.check_agents(_matrix(), _agent_dir(tmp_path, declared_tools=["catalog.get_product"]))
    assert errors == [] and _codes(warnings) == ["UNDECLARED"]


def test_unregistered_is_error_unless_flagged(tmp_path):
    errors, _ = gen_policy.check_agents(_matrix(), _agent_dir(tmp_path, agent_id="ghost"))
    assert _codes(errors) == ["UNREGISTERED"]

    errors, warnings = gen_policy.check_agents(_matrix(), _agent_dir(tmp_path, agent_id="ghost", unregistered=True))
    assert errors == [] and _codes(warnings) == ["UNREGISTERED"]


def test_unknown_tool_in_agent_config(tmp_path):
    errors, _ = gen_policy.check_agents(
        _matrix(), _agent_dir(tmp_path, declared_tools=["catalog.get_product", "refunds.issue_refund", "x.nope"])
    )
    assert "UNKNOWN_TOOL" in _codes(errors)


def test_unknown_tool_in_matrix(tmp_path):
    m = _matrix(tools=["catalog.get_product", "refunds.issue_refund", "catalog.missing"])
    errors, _ = gen_policy.check_agents(m, _agent_dir(tmp_path))
    assert "UNKNOWN_TOOL" in _codes(errors)


def test_role_mismatch(tmp_path):
    errors, _ = gen_policy.check_agents(_matrix(), _agent_dir(tmp_path, agent_role="refunds"))
    assert _codes(errors) == ["ROLE_MISMATCH"]


def test_route_not_granted(tmp_path):
    errors, _ = gen_policy.check_agents(_matrix(), _agent_dir(tmp_path, model_route="premium"))
    assert _codes(errors) == ["ROUTE_NOT_GRANTED"]


def test_tools_policy_uses_require_obo_roles_and_ends_with_deny():
    rules = gen_policy.build_tools_policy(_matrix())["rules"]
    refund = next(r for r in rules if r["name"] == "allow-shopper-refunds-issue-refund")
    assert refund["match"]["jwt_claims"] == {"agent_role": ["shopper"], "obo_role": ["care_supervisor"]}
    assert refund["match"]["headers"] == {"Mcp-Method": ["tools/call"], "Mcp-Name": ["issue_refund"]}
    assert rules[-1] == {"name": "default-deny", "action": "deny", "match": "any"}


def test_llm_policy_allows_only_post_converse():
    rule = gen_policy.build_llm_policy(_matrix())["rules"][0]
    assert rule["match"]["path_exact"] == "/v1/converse"
    assert rule["match"]["method"] == "POST"
    assert rule["match"]["headers"] == {"x-model-route": ["fast"]}


def _rl_inputs(adjust=10, default=60, llm=30):
    m = {"rate_limits": {
        "tools_default_per_agent_identity": {"requests": 60, "per": "minute"},
        "llm_default_per_agent_identity": {"requests": 30, "per": "minute"},
        "overrides": [{"match": {"tool": "inventory.adjust_stock"}, "limit": {"requests": 10, "per": "minute"}}],
    }}
    security = {"rate_limiter_policies": [{"name": "rl-tools", "rules": [
        {"match": {"header": {"Mcp-Name": "adjust_stock"}}, "limit": {"requests": adjust, "per": "minute"}},
        {"match": "any", "limit": {"requests": default, "per": "minute"}},
    ]}]}
    llm_lb = {"name": "llm-lb", "rate_limit": {"path": "/v1/", "requests": llm, "per": "minute"}}
    lbs = {"http_load_balancers": [llm_lb]}
    return m, security, lbs


def test_rate_limits_in_sync():
    assert gen_policy.check_rate_limits(*_rl_inputs()) == []


def test_rate_limit_drift_is_reported():
    assert _codes(gen_policy.check_rate_limits(*_rl_inputs(adjust=20))) == ["RL_DRIFT"]
    assert _codes(gen_policy.check_rate_limits(*_rl_inputs(default=100))) == ["RL_DRIFT"]
    assert _codes(gen_policy.check_rate_limits(*_rl_inputs(llm=99))) == ["RL_DRIFT"]


def test_stale_generated_file_fails_check(tmp_path):
    out = tmp_path / "generated.yaml"
    assert gen_policy.main([], out=out) == 0
    assert gen_policy.main(["--check"], out=out) == 0
    out.write_text(out.read_text() + "# hand edit\n")
    assert gen_policy.main(["--check"], out=out) == 1


def test_repo_matrix_has_only_the_two_expected_warnings():
    m = gen_policy.load_yaml(gen_policy.MATRIX)
    errors, warnings = gen_policy.check_agents(m)
    assert errors == []
    assert sorted(_codes(warnings)) == ["OVERDECLARED", "UNREGISTERED"]
