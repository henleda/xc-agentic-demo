"""The local-gateway reads the real XC intent files. These tests pin what it takes from them and what it refuses."""

import shutil
from pathlib import Path

import pytest
import yaml
from local_gateway.intent import EMULATED, NOT_EMULATED, WHERE_SERVED, IntentError, load_lb, load_pools
from local_gateway.policy import NO_MATCH, RequestFacts, decide

REPO = Path(__file__).resolve().parents[4]
INTENT = REPO / "deploy/xc/intent"
POOLS = load_pools(REPO / "deploy/local/pools.yaml")
ENV = {"GATEWAY_API_KEY": "test-gateway-key"}
ALL_LBS = [lb["name"] for lb in yaml.safe_load((INTENT / "20-load-balancers.yaml").read_text())["http_load_balancers"]]
NOT_RUN_LOCALLY = {"cockpit-lb"}  # the local cockpit is published on 127.0.0.1:8095 with no gateway in front


def facts(method="POST", path="/", headers=None, claims=None) -> RequestFacts:
    lists = {name.lower(): (value if isinstance(value, list) else [value]) for name, value in (headers or {}).items()}
    return RequestFacts(method, path, lists, claims)


@pytest.mark.parametrize("lb_name", sorted(set(ALL_LBS) - NOT_RUN_LOCALLY))
def test_every_local_lb_in_the_intent_loads(lb_name):
    """A new intent field fails here first: decide whether the local-gateway emulates it (intent.py)."""
    lb = load_lb(lb_name, INTENT, POOLS, ENV)
    assert lb.routes


def test_cockpit_lb_source_ip_rule_is_refused_not_ignored():
    with pytest.raises(IntentError, match="source_ip_prefixes"):
        load_lb("cockpit-lb", INTENT, POOLS, ENV)


def test_every_intent_pool_has_a_local_url():
    pools = yaml.safe_load((INTENT / "10-origin-pools.yaml").read_text())
    names = {p["name"] for p in pools["k8s_service_pools"]} | {p["name"] for p in pools["ip_pools"]}
    assert names == set(POOLS)


def test_every_local_pool_url_matches_its_compose_service():
    """The in-process tests route by hostname only, so a wrong port or scheme in pools.yaml would pass them. Check
    each pool against the port its compose service's healthcheck uses. Services not built yet must be absent."""
    import re

    compose = yaml.safe_load((REPO / "deploy/local/docker-compose.yaml").read_text())["services"]
    for pool, url in POOLS.items():
        match = re.fullmatch(r"http://([a-z0-9-]+):(\d+)", url)
        assert match, f"{pool}: {url} must be http://<compose service>:<port>"
        service, port = match.group(1), match.group(2)
        if service not in compose:
            continue  # added in a later step; the gateway answers 503 for it until then
        health = " ".join(compose[service]["healthcheck"]["test"])
        assert f"127.0.0.1:{port}/" in health, f"{pool} -> {url}, but {service} listens on {health}"


def test_some_pools_point_at_running_services():
    compose = yaml.safe_load((REPO / "deploy/local/docker-compose.yaml").read_text())["services"]
    running = [pool for pool, url in POOLS.items() if url.split("//")[1].split(":")[0] in compose]
    assert {"op-idp", "op-erp", "op-mcp-catalog", "op-mcp-orders", "op-mcp-inventory", "op-mcp-refunds"} <= set(running)


def test_field_lists_do_not_overlap():
    assert not (EMULATED & NOT_EMULATED) and not (EMULATED & WHERE_SERVED) and not (NOT_EMULATED & WHERE_SERVED)


def test_tools_lb_uses_the_generated_policy_and_names_what_it_skips():
    lb = load_lb("tools-lb", INTENT, POOLS, ENV)
    assert lb.policy.name == "sp-tools"
    assert lb.jwt.audience == "copperline-tools"
    assert lb.jwt.mandatory_claims == ("agent_role", "obo_role", "agent_id")
    assert {"waf", "data_guard", "rate_limiter_policies", "openapi_validation"} <= set(lb.not_emulated)


def test_llm_lb_swaps_the_gateway_key_without_needing_it_from_the_agent():
    lb = load_lb("llm-lb", INTENT, POOLS, ENV)
    assert lb.headers_to_remove == ["x-api-key"]
    assert lb.headers_to_add == [("x-api-key", "test-gateway-key")]


def test_llm_lb_refuses_to_start_without_the_gateway_key():
    with pytest.raises(IntentError, match="GATEWAY_API_KEY"):
        load_lb("llm-lb", INTENT, POOLS, {})


def test_ops_lb_rewrites_the_agent_prefix():
    lb = load_lb("ops-lb", INTENT, POOLS, ENV)
    route = next(r for r in lb.routes if r.matches("/storeops/v1/chat"))
    assert (route.pool, route.upstream_path("/storeops/v1/chat")) == ("op-storeops", "/v1/chat")


def test_unknown_lb_field_stops_the_gateway(tmp_path):
    intent = _copy_intent(tmp_path)
    data = yaml.safe_load((intent / "20-load-balancers.yaml").read_text())
    data["http_load_balancers"][0]["bot_defense"] = True
    (intent / "20-load-balancers.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(IntentError, match="bot_defense"):
        load_lb(data["http_load_balancers"][0]["name"], intent, POOLS, ENV)


def test_unknown_policy_matcher_stops_the_gateway(tmp_path):
    intent = _copy_intent(tmp_path)
    data = yaml.safe_load((intent / "30-security.yaml").read_text())
    data["service_policies_static"][0]["rules"][0]["source_asn"] = [64512]
    (intent / "30-security.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(IntentError, match="source_asn"):
        load_lb("ops-lb", intent, POOLS, ENV)


def test_route_to_a_pool_without_a_local_url_stops_the_gateway():
    pools = {k: v for k, v in POOLS.items() if k != "op-mcp-orders"}
    with pytest.raises(IntentError, match="op-mcp-orders"):
        load_lb("tools-lb", INTENT, pools, ENV)


# ---- policy evaluation against the real generated sp-tools ----------------------------------------------------------


@pytest.fixture(scope="module")
def sp_tools():
    return load_lb("tools-lb", INTENT, POOLS, ENV).policy


def tool_call(server, name, agent_role, obo_role):
    return facts(path=f"/mcp/{server}", headers={"Mcp-Method": "tools/call", "Mcp-Name": name},
                 claims={"agent_role": agent_role, "obo_role": obo_role, "agent_id": agent_role})


def test_s0_shopper_may_read_order_status(sp_tools):
    assert decide(sp_tools, tool_call("orders", "get_order_status", "shopper", "customer"))[0] == "allow"


def test_s1_shopper_may_not_issue_refunds(sp_tools):
    assert decide(sp_tools, tool_call("refunds", "issue_refund", "shopper", "customer")) == ("deny", "default-deny")


def test_s1b_policy_sees_only_the_header(sp_tools):
    """The honest gap: the fabric matches Mcp-Name, so a forbidden body tool behind an allowed header passes."""
    assert decide(sp_tools, tool_call("orders", "get_order_status", "shopper", "customer"))[0] == "allow"


def test_s3_issue_refund_needs_a_supervisor(sp_tools):
    assert decide(sp_tools, tool_call("refunds", "issue_refund", "refunds", "care_agent"))[0] == "deny"
    assert decide(sp_tools, tool_call("refunds", "issue_refund", "refunds", "care_supervisor"))[0] == "allow"


def test_s5_machine_token_is_denied(sp_tools):
    assert decide(sp_tools, tool_call("orders", "list_customer_orders", "storeops", "none"))[0] == "deny"


def test_a_repeated_policy_header_never_matches(sp_tools):
    two_names = facts(path="/mcp/orders", headers={"Mcp-Method": "tools/call",
                                                    "Mcp-Name": ["get_order_status", "get_order_details"]},
                      claims={"agent_role": "shopper", "obo_role": "customer"})
    assert decide(sp_tools, two_names)[0] == "deny"


def test_wrong_method_or_path_is_denied(sp_tools):
    get = facts(method="GET", path="/mcp/orders", headers={"Mcp-Method": "tools/call", "Mcp-Name": "get_order_status"},
                claims={"agent_role": "shopper", "obo_role": "customer"})
    assert decide(sp_tools, get)[0] == "deny"
    sub_path = tool_call("orders", "get_order_status", "shopper", "customer")
    sub_path.path = "/mcp/orders/export"
    assert decide(sp_tools, sub_path)[0] == "deny"


def test_static_policies_read_both_shapes():
    sp_erp = load_lb("erp-lb", INTENT, POOLS, ENV).policy
    tool_server = {"svc_role": "tool-server"}
    assert decide(sp_erp, facts("GET", "/orders/CO-1042", claims=tool_server))[0] == "allow"
    assert decide(sp_erp, facts("GET", "/admin/state", claims=tool_server))[0] == "deny"
    assert decide(sp_erp, facts("GET", "/orders/CO-1042", claims={"svc_role": "other"}))[0] == "deny"

    sp_idp = load_lb("idp-lb", INTENT, POOLS, ENV).policy
    assert decide(sp_idp, facts("POST", "/token/exchange"))[0] == "allow"
    assert decide(sp_idp, facts("GET", "/.well-known/jwks.json"))[0] == "allow"
    assert decide(sp_idp, facts("POST", "/auth/demo-login")) == ("deny", "sp-idp[3]")


def test_no_rule_matched_is_deny():
    sp_ops = load_lb("ops-lb", INTENT, POOLS, ENV).policy
    sp_ops.rules = [r for r in sp_ops.rules if r.action == "allow"]
    assert decide(sp_ops, facts("GET", "/nowhere")) == ("deny", NO_MATCH)


def _copy_intent(tmp_path) -> Path:
    target = tmp_path / "intent"
    shutil.copytree(INTENT, target)
    return target
