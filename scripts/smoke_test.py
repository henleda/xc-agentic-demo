#!/usr/bin/env python3
"""A fast go/no-go check of every hop, before running scenarios. Each line is one check; any FAIL exits 1.

Usage:
    uv run python scripts/smoke_test.py --env local     # the compose stack (make local-up); make local-smoke

Every check sends a real request through the load balancer it names and requires an answer from the service behind
it (x-copperline-origin), so "ok" means that hop works, not just that a gateway process is up. Nothing depends on what
a model chooses or on what an agent has cached, so the result is the same in stub mode, on real Bedrock and when it is
run twice in a row.

    gateways          every load balancer process answers (its own health path)
    origins           a real request through each LB reaches its pool: the IdP (shop-lb, ops-lb, idp-lb, partner-lb),
                      each agent (shop-lb, ops-lb), each MCP server (tools-lb, with a real agent token), partner-api
                      (partner-lb, with the real partner token), the ERP (erp-lb, with a real service token)
    S0 end to end     alex.rivera's S0 turn through shop-lb: a token exchange, an RE-local tool (catalog), a CE tool
                      that reads the ERP (orders -> idp-lb -> erp-lb -> erp-mock) and a model call, all 200 and none an
                      MCP isError. S0 is scripted, so the same steps run in stub mode and on real Bedrock
    policy            erp-lb refuses /admin/ even with a valid service token (sp-erp), and the admin ports are loopback

--env tenant (Phase 2): the same through the public front doors, plus "erp.agentic-demo does not resolve from an RE
pod" (debug Job) and --no-security. Not built yet; it exits 2 and says so.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from copperline_common.mcp import META_PROTOCOL_VERSION, PROTOCOL_VERSION

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_env  # noqa: E402

GATEWAY_HEALTH = "/__local-gateway/healthz"
ORIGIN = "x-copperline-origin"
S0_MESSAGE = "Hi, where is my order CO-1042? Also, is the Alpine 2 tent any good?"  # scenarios/S0-baseline.yaml


class Smoke:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail and not ok else ''}")
        self.failures += 0 if ok else 1
        return ok


def send(client: httpx.Client, method: str, url: str, **kwargs) -> tuple[httpx.Response | None, str]:
    """(response, problem). Never raises: a down hop, a timeout or a refused connection is a FAIL line, not a crash."""
    try:
        return client.request(method, url, **kwargs), ""
    except httpx.TimeoutException as e:
        return None, f"timed out ({type(e).__name__})"
    except httpx.HTTPError as e:
        return None, f"no response: {type(e).__name__} (is the stack up? make local-up)"


def answered_by(response: httpx.Response | None, origin: str, status: int, problem: str) -> tuple[bool, str]:
    """Did `origin` itself answer with `status`? (The gateway's own answers carry no x-copperline-origin.)"""
    if response is None:
        return False, problem
    got = response.headers.get(ORIGIN)
    if response.status_code == status and got == origin:
        return True, ""
    who = got or f"the gateway ({response.headers.get('x-local-gateway', 'no origin header')})"
    return False, f"HTTP {response.status_code} from {who}, expected {status} from {origin}"


def json_of(response: httpx.Response | None) -> dict:
    try:
        body = response.json() if response is not None else {}
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def run_local(env: demo_env.DemoEnv) -> int:
    smoke = Smoke()
    secrets = demo_env.read_secrets()
    shop, ops, partner = env.front_doors["shop-lb"], env.front_doors["ops-lb"], env.front_doors["partner-lb"]
    with httpx.Client(timeout=120) as client:
        print(" gateways")
        lbs = {**env.front_doors, "tools-lb": env.tools_base_url, "llm-lb": env.llm_base_url,
               "idp-lb": env.idp_base_url, "erp-lb": env.erp_lb_url}
        for name, base in sorted(lbs.items()):
            response, problem = send(client, "GET", f"{base}{GATEWAY_HEALTH}")
            smoke.check(f"{name} is up", response is not None and response.status_code == 200,
                        problem or f"HTTP {response.status_code}")
        for name, url in (("ERP admin", f"{env.erp_admin_url}/healthz"),
                          ("AI gateway admin", f"{env.aigw_admin_url}/healthz")):
            response, problem = send(client, "GET", url)
            smoke.check(f"{name} answers on loopback", response is not None and response.status_code == 200,
                        problem or f"HTTP {response.status_code}")

        print(" origins (a real request through each LB, answered by the service behind it)")
        tokens = {}
        for door, sub, name in ((shop, "alex.rivera", "shop-lb -> idp: alex.rivera logs in"),
                                (ops, "jordan.lee", "ops-lb -> idp: jordan.lee logs in"),
                                (ops, "sam.patel", "ops-lb -> idp: sam.patel logs in")):
            response, problem = send(client, "POST", f"{door}/auth/demo-login", json={"sub": sub})
            ok, detail = answered_by(response, "idp", 200, problem)
            if smoke.check(name, ok, detail):
                tokens[sub] = json_of(response).get("access_token")
        for path, sub, agent in (("/storeops/v1/chat", "jordan.lee", "storeops"),
                                 ("/refunds/v1/chat", "sam.patel", "refunds")):
            # An empty chat is refused by the agent itself (400): proves ops-lb routes to it, with no model call.
            response, problem = send(client, "POST", f"{ops}{path}", json={},
                                     headers={"Authorization": f"Bearer {tokens.get(sub, '')}"})
            smoke.check(f"ops-lb -> {agent} agent", *answered_by(response, f"{agent}-agent", 400, problem))

        agent_tokens = exchange_tokens(client, env, tokens, secrets, smoke)
        for server, agent in (("catalog", "shopper"), ("orders", "shopper"), ("inventory", "storeops"),
                              ("refunds", "refunds")):
            response, problem = mcp_discover(client, env, server, agent_tokens.get(agent))
            smoke.check(f"tools-lb -> mcp-{server} (as the {agent} agent)",
                        *answered_by(response, f"mcp-{server}", 200, problem))

        partner_token, problem = client_credentials(client, f"{partner}/auth/token", "granite-supply",
                                                    secrets.get("GRANITE_SUPPLY_SECRET"), "copperline-partner")
        smoke.check("partner-lb -> idp: granite-supply gets a token", bool(partner_token), problem)
        response, problem = send(client, "GET", f"{partner}/v1/purchase-orders",
                                 headers={"Authorization": f"Bearer {partner_token or ''}"})
        smoke.check("partner-lb -> partner-api", *answered_by(response, "partner-api", 200, problem))

        svc_token, problem = client_credentials(client, f"{env.idp_base_url}/auth/token", "svc-mcp",
                                                secrets.get("SVC_MCP_SECRET"), "copperline-erp")
        smoke.check("idp-lb -> idp: the tool servers' service token", bool(svc_token), problem)
        response, problem = send(client, "GET", f"{env.erp_lb_url}/orders/CO-1042",
                                 headers={"Authorization": f"Bearer {svc_token or ''}"})
        smoke.check("erp-lb -> erp-mock", *answered_by(response, "erp-mock", 200, problem))

        print(" S0 end to end (scripted: the same steps in stub mode and on real Bedrock)")
        response, problem = send(client, "POST", f"{shop}/v1/chat",
                                 json={"messages": [{"role": "user", "content": S0_MESSAGE}], "scenario_hint": "S0"},
                                 headers={"Authorization": f"Bearer {tokens.get('alex.rivera', '')}"})
        body = json_of(response)
        if smoke.check("alex.rivera's S0 turn completes", response is not None and response.status_code == 200
                       and body.get("scenario") == "S0",
                       problem or f"HTTP {response.status_code}, scenario {body.get('scenario')!r}"):
            trace = body.get("trace") or []
            for name, wanted in (("catalog tool (RE-local)", "catalog.get_reviews"),
                                 ("orders tool, which reads the ERP (orders -> idp-lb -> erp-lb)",
                                  "orders.get_order_status")):
                step = next((s for s in trace if s.get("tool") == wanted), None)
                smoke.check(name, step is not None and step["status"] == 200 and not step.get("blocked_by")
                            and step.get("is_error") is False, describe(step))
            model = [s for s in trace if s.get("kind") == "model"]
            smoke.check("model call through llm-lb -> ai-gw",
                        bool(model) and all(s["status"] == 200 and not s.get("blocked_by") for s in model),
                        describe(next((s for s in model if s["status"] != 200), None)) if model else "not in trace")

        print(" policy")
        response, problem = send(client, "GET", f"{env.erp_lb_url}/admin/state",
                                 headers={"Authorization": f"Bearer {svc_token or ''}"})
        refused = (response is not None and response.status_code == 403 and ORIGIN not in response.headers
                   and response.headers.get("x-local-gateway") == "erp-lb")
        smoke.check("erp-lb refuses /admin/ even with a valid service token (sp-erp)", bool(svc_token) and refused,
                    problem or ("no service token" if not svc_token else
                                f"HTTP {response.status_code} from {response.headers.get(ORIGIN) or 'the gateway'}"))
    print("smoke: " + ("all ok" if not smoke.failures else f"{smoke.failures} failed"))
    return 1 if smoke.failures else 0


def describe(step: dict | None) -> str:
    if step is None:
        return "not in the trace"
    if step.get("is_error"):
        return f"tool error: {(step.get('result_preview') or '')[:120]}"
    return f"{step.get('status')} {step.get('blocked_by') or ''} {step.get('reason') or ''}".strip()


def client_credentials(client: httpx.Client, url: str, client_id: str, secret: str | None,
                       audience: str) -> tuple[str | None, str]:
    if not secret:
        return None, f"no secret for {client_id} in .secrets/secrets.env (scripts/gen_keys.py)"
    response, problem = send(client, "POST", url, data={"grant_type": "client_credentials", "client_id": client_id,
                                                         "client_secret": secret, "audience": audience})
    if response is None:
        return None, problem
    token = json_of(response).get("access_token")
    return (token, "") if response.status_code == 200 and token else (None, f"HTTP {response.status_code}")


def exchange_tokens(client: httpx.Client, env: demo_env.DemoEnv, user_tokens: dict, secrets: dict,
                    smoke: Smoke) -> dict[str, str | None]:
    """Agent tokens for the MCP checks, exchanged at idp-lb as the agents do (independent of any agent's cache)."""
    result = {}
    for agent, sub in (("shopper", "alex.rivera"), ("storeops", "jordan.lee"), ("refunds", "sam.patel")):
        client_id = f"agent-{agent}"
        form = {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": user_tokens.get(sub) or "",
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt", "client_id": client_id,
                "client_secret": secrets.get(f"AGENT_{agent.upper()}_SECRET", ""),
                "requested_audience": "copperline-tools"}
        response, problem = send(client, "POST", f"{env.idp_base_url}/token/exchange", data=form)
        token = json_of(response).get("access_token") if response is not None else None
        ok, detail = answered_by(response, "idp", 200, problem)
        if smoke.check(f"idp-lb -> idp: the {agent} agent exchanges {sub}'s token", ok and bool(token), detail):
            result[agent] = token
    return result


def mcp_discover(client: httpx.Client, env: demo_env.DemoEnv, server: str, token: str | None):
    """An MCP server/discover through tools-lb: allowed for each agent on its own servers, never touches the ERP."""
    body = {"jsonrpc": "2.0", "id": "smoke", "method": "server/discover",
            "params": {"_meta": {META_PROTOCOL_VERSION: PROTOCOL_VERSION}}}
    headers = {"Authorization": f"Bearer {token or ''}", "MCP-Protocol-Version": PROTOCOL_VERSION,
               "Mcp-Method": "server/discover", "Accept": "application/json, text/event-stream"}
    return send(client, "POST", f"{env.tools_base_url}/mcp/{server}", json=body, headers=headers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", required=True, choices=["local", "tenant"])
    parser.add_argument("--no-security", action="store_true", help="tenant, Phase 2: plain LBs, no XC controls")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # over `make devbox-run` (no tty) each line shows as it happens
    if args.env == "tenant":
        print("--env tenant is not built yet (Phase 2).", file=sys.stderr)
        return 2
    print("smoke, env local:")
    return run_local(demo_env.local())


if __name__ == "__main__":
    sys.exit(main())
