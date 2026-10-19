"""Live checks against the running compose stack (`make local-up`, then `make local-check`). NOT XC.

Skipped unless LOCAL_STACK=1, so `make check` never needs Docker. Everything goes through the published local-gateway
ports, the way agents will call the internal LBs: the same scenario plans and expect.local blocks as the in-process
test, plus proof that every container runs the way vK8s will run it.
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from copperline_common.credentials import client_secret
from copperline_common.expect import evaluate, select_block
from copperline_common.outbound import new_http_client
from copperline_common.personas import Personas
from copperline_common.plan_executor import ExecContext, run_plan
from copperline_common.scenarios import by_id, load_scenarios
from copperline_common.tokens import exchange

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("LOCAL_STACK") != "1", reason="needs `make local-up`; run `make local-check`"),
]

REPO = Path(__file__).resolve().parents[4]
PROJECT = "copperline-local"
FRONT_DOORS = {"shop-lb": "http://127.0.0.1:8001", "ops-lb": "http://127.0.0.1:8002", "partner-lb": "http://127.0.0.1:8003"}
TOOLS, LLM, IDP, ERP = "http://127.0.0.1:8011", "http://127.0.0.1:8012", "http://127.0.0.1:8013", "http://127.0.0.1:8014"
ERP_ADMIN = "http://127.0.0.1:8181"
GATEWAYS = {"tools-lb": TOOLS, "llm-lb": LLM, "idp-lb": IDP, "erp-lb": ERP, **FRONT_DOORS}
AGENT_CLIENTS = {"shopper": "agent-shopper", "storeops": "agent-storeops", "refunds": "agent-refunds"}
SCENARIOS = load_scenarios(REPO / "scenarios")
PERSONAS = Personas.load(REPO / "config/personas.yaml")
# The exfil URL must not resolve: it stands in for S5's network policy. (Compose has no egress policy yet.)
ENV = {"TOOLS_BASE_URL": TOOLS, "IDP_BASE_URL": IDP, "LLM_BASE_URL": LLM, "PARTNER_BASE_URL": FRONT_DOORS["partner-lb"],
       "EXFIL_TEST_URL": "https://exfil.invalid/rows"}


@pytest.fixture(autouse=True)
def client_secrets(monkeypatch):
    """Plan token steps and exchanges read client secrets from env, as agent-runtime will."""
    for line in (REPO / ".secrets/secrets.env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            monkeypatch.setenv(name, value)


async def estate(http) -> dict:
    return (await http.get(f"{ERP_ADMIN}/admin/state")).json()


async def run_scenario(scenario_id: str) -> tuple[list[dict], dict]:
    scenario = by_id(SCENARIOS, scenario_id)
    async with new_http_client(timeout=15) as http:
        tokens: dict[str, str] = {}
        obo_sub = None
        if not scenario.persona.startswith(("job:", "partner:")):
            door = "shop-lb" if PERSONAS.get(scenario.persona)["role"] == "customer" else "ops-lb"
            login = await http.post(f"{FRONT_DOORS[door]}/auth/demo-login", json={"sub": scenario.persona})
            assert login.status_code == 200, login.text
            for audience in ("copperline-tools", "copperline-llm"):
                result = await exchange(http, IDP, client_id=AGENT_CLIENTS[scenario.agent],
                                        client_secret=client_secret(AGENT_CLIENTS[scenario.agent]),
                                        subject_token=login.json()["access_token"], requested_audience=audience)
                assert result.token, result.http.body_text
                tokens[f"aud:{audience}"] = result.token
            obo_sub = scenario.persona
        before = await estate(http)
        ctx = ExecContext(http=http, tools_base_url=TOOLS, llm_base_url=LLM, idp_base_url=IDP, tokens=tokens,
                          agent_id=scenario.agent, obo_sub=obo_sub, scenario_id=scenario.id, env=ENV)
        run = await run_plan(scenario, ctx)
        after = await estate(http)
    delta = {key: after[key] - before[key] for key in ("refunds_issued", "stock_writes")}
    return [step.to_dict() for step in run.steps], delta


@pytest.mark.parametrize("lb, base", sorted(GATEWAYS.items()))
def test_every_gateway_is_up_and_says_it_is_not_xc(lb, base):
    body = httpx.get(f"{base}/__local-gateway/healthz", timeout=5).json()
    assert body["lb"] == lb and "NOT XC" in body["note"]


@pytest.mark.parametrize("scenario_id", ["S0", "S1", "S1b", "S2", "S3a", "S3b", "S5"])
def test_expect_local_passes_against_the_containers(scenario_id):
    trace, delta = asyncio.run(run_scenario(scenario_id))
    result = evaluate(select_block(by_id(SCENARIOS, scenario_id).expect, "local"), trace, estate_delta=delta)
    failed = [f"{c.description}: {c.detail}" for c in result.checks if not c.ok]
    assert result.ok, "\n".join(failed) + "\n" + json.dumps(trace, indent=1)[:4000]


def test_denials_come_from_the_gateway_and_s1b_from_the_server():
    s1, _ = asyncio.run(run_scenario("S1"))
    s1b, _ = asyncio.run(run_scenario("S1b"))
    refund = next(s for s in s1 if s["i"] == 2)
    assert refund["reason"].startswith("local-gateway (NOT XC)")
    assert (s1b[0]["status"], s1b[0]["blocked_by"]) == (400, "server") and "HeaderMismatch" in s1b[0]["reason"]


def test_erp_admin_is_published_but_erp_lb_refuses_it():
    assert httpx.get(f"{ERP_ADMIN}/admin/state", timeout=5).status_code == 200
    denied = httpx.get(f"{ERP}/admin/state", timeout=5)
    assert denied.status_code == 401 and denied.headers["x-local-gateway"] == "erp-lb"  # JWT is checked first


def test_mcp_servers_never_had_to_deny():
    """Defense in depth must not be what stopped anything: the gateway denies first."""
    logs = subprocess.run(["docker", "compose", "-p", PROJECT, "logs", "--no-color", "mcp-catalog", "mcp-orders",
                           "mcp-inventory", "mcp-refunds"], capture_output=True, text=True, check=True).stdout
    assert "server_authz_deny" not in logs


def test_every_container_runs_like_a_vk8s_pod():
    ids = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"],
                         capture_output=True, text=True, check=True).stdout.split()
    assert len(ids) == 13, "expected idp, erp-mock, 4 MCP servers and 7 gateways"
    fmt = ("{{json .Name}} {{json .Config.User}} {{json .HostConfig.ReadonlyRootfs}} {{json .HostConfig.Privileged}} "
           "{{json .HostConfig.CapDrop}} {{json .HostConfig.Tmpfs}} {{json .HostConfig.SecurityOpt}}")
    lines = subprocess.run(["docker", "inspect", "--format", fmt, *ids], capture_output=True, text=True,
                           check=True).stdout.splitlines()
    for line in lines:
        name, user, read_only, privileged, cap_drop, tmpfs, security_opt = (json.loads(f) for f in line.split(" "))
        assert (user, read_only, privileged, cap_drop) == ("1000770000:0", True, False, ["ALL"]), name
        assert "/tmp" in (tmpfs or {}), f"{name}: no tmpfs /tmp"
        assert "no-new-privileges:true" in (security_opt or []), f"{name}: no no-new-privileges"



@pytest.mark.parametrize("host", ["tools.agentic-demo", "llm.agentic-demo", "idp.agentic-demo", "erp.agentic-demo"])
def test_internal_lbs_answer_on_their_vk8s_urls_inside_the_network(host):
    """Services call http://<name>.agentic-demo with no port, as in vK8s (XC internal LBs listen on 80)."""
    url = f"http://{host}/__local-gateway/healthz"
    probe = f"import json, urllib.request; print(json.load(urllib.request.urlopen({url!r}, timeout=3))['lb'])"
    out = subprocess.run(["docker", "compose", "-p", PROJECT, "exec", "-T", "mcp-orders", "python", "-c", probe],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == host.split(".")[0] + "-lb"
