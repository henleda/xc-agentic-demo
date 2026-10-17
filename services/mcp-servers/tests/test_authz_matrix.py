"""The server's defense-in-depth check must make exactly the decision XC's generated sp-tools makes.

Both come from config/policy/tool-access-matrix.yaml; this test proves they agree for every combination, so the
"fabric decides, server double-checks" story holds. If it fails, the matrix, gen_policy.py or authz.py drifted.
"""

import itertools
from pathlib import Path

import gen_policy  # scripts/ is on pytest's pythonpath
import pytest
import yaml
from mcp_servers.authz import Matrix

REPO = Path(__file__).resolve().parents[3]
MATRIX = yaml.safe_load((REPO / "config/policy/tool-access-matrix.yaml").read_text())
TOOLS = sorted(MATRIX["tools"])
AGENT_ROLES = sorted({a["agent_role"] for a in MATRIX["agents"].values()}) + ["insights-nightly"]
OBO_ROLES = ["customer", "store_associate", "care_agent", "care_supervisor", "partner", "none"]


def sp_tools_allows(agent_role: str, fq_tool: str, obo_role: str) -> bool:
    """Evaluate the generated sp-tools rules (first match wins) for a tools/call."""
    server, short = fq_tool.split(".", 1)
    path = MATRIX["tool_servers"][server]["path"]
    for rule in gen_policy.build_tools_policy(MATRIX)["rules"]:
        match = rule["match"]
        if match == "any":
            return rule["action"] == "allow"
        if (match["path_exact"] == path and agent_role in match["jwt_claims"]["agent_role"]
                and obo_role in match["jwt_claims"]["obo_role"]
                and "tools/call" in match["headers"]["Mcp-Method"]
                and short in match["headers"].get("Mcp-Name", [])):
            return rule["action"] == "allow"
    return False


@pytest.mark.parametrize("agent_role, fq_tool, obo_role", list(itertools.product(AGENT_ROLES, TOOLS, OBO_ROLES)))
def test_server_matches_generated_service_policy(agent_role, fq_tool, obo_role):
    assert Matrix(MATRIX).may_call(agent_role, fq_tool, obo_role) == sp_tools_allows(agent_role, fq_tool, obo_role)


def test_the_table_is_not_trivially_all_deny():
    allowed = [combo for combo in itertools.product(AGENT_ROLES, TOOLS, OBO_ROLES) if sp_tools_allows(*combo)]
    assert ("refunds", "refunds.issue_refund", "care_supervisor") in allowed  # S3b
    assert ("refunds", "refunds.issue_refund", "care_agent") not in allowed  # S3a
    assert ("shopper", "refunds.issue_refund", "customer") not in allowed  # S1
    assert not [combo for combo in allowed if combo[2] == "none"]  # S5: machine tokens never allowed


def test_over_the_wire_a_denied_combination_is_403_and_never_reaches_the_erp(server, agent_token, mcp):
    """One end-to-end sample of the table above, through the real router and ERP."""
    inventory, world = server("inventory")
    resp = mcp.call(inventory, "inventory", "adjust_stock", {"sku": "TNT-ALPINE-2", "store_id": "S-001", "delta": 1},
                    agent_token("refunds", "care_supervisor"))
    assert resp.status_code == 403 and resp.json()["error"]["code"] == -32003
    assert world.erp_calls == []


@pytest.mark.parametrize(
    "agent_role, obo_role, server_name, allowed",
    [("shopper", "customer", "catalog", True), ("shopper", "customer", "orders", True),
     ("shopper", "customer", "refunds", False), ("refunds", "care_agent", "refunds", True),
     ("refunds", "care_agent", "inventory", False), ("storeops", "none", "orders", False)],
)
def test_discovery_follows_the_matrix(server, agent_token, mcp, agent_role, obo_role, server_name, allowed):
    client, _ = server(server_name)
    for method in ("tools/list", "server/discover"):
        resp = mcp.rpc(client, server_name, method, agent_token(agent_role, obo_role))
        assert (resp.status_code == 200) is allowed, (method, resp.text)
        if not allowed:
            assert resp.status_code == 403
