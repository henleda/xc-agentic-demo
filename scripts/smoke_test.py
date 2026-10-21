#!/usr/bin/env python3
"""A fast go/no-go check of every hop, before running scenarios. Each line is one check; any FAIL exits 1.

Usage:
    uv run python scripts/smoke_test.py --env local     # the compose stack (make local-up)

Checks (local):
    every load balancer answers on its health path          (front doors and internal LBs)
    the admin ports answer on loopback                      (ERP 8181, AI gateway 8190)
    a customer logs in at shop-lb                            user -> front door -> IdP
    the shopper agent answers a chat, with a token exchange, a tool call and a model call that all succeed
                                                             agent -> idp-lb, tools-lb -> MCP -> erp-lb, llm-lb -> ai-gw
    the ERP admin port is NOT reachable through erp-lb       (only the loopback publish serves it)

--env tenant (Phase 2): the same through the public front doors, plus "erp.agentic-demo does not resolve from an RE
pod" (debug Job) and --no-security. Not built yet; it exits 2 and says so.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_env  # noqa: E402

GATEWAY_HEALTH = "/__local-gateway/healthz"


class Smoke:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail and not ok else ''}")
        self.failures += 0 if ok else 1
        return ok


def get(client: httpx.Client, url: str, **kwargs) -> httpx.Response | None:
    try:
        return client.get(url, **kwargs)
    except httpx.HTTPError:
        return None


def run_local(env: demo_env.DemoEnv) -> int:
    smoke = Smoke()
    with httpx.Client(timeout=15) as client:
        lbs = {**env.front_doors, "tools-lb": env.tools_base_url, "llm-lb": env.llm_base_url,
               "idp-lb": env.idp_base_url}
        for name, base in sorted(lbs.items()):
            response = get(client, f"{base}{GATEWAY_HEALTH}")
            smoke.check(f"{name} answers", response is not None and response.status_code == 200,
                        "no response (is the stack up? make local-up)" if response is None else
                        f"HTTP {response.status_code}")
        for name, url in (("ERP admin", f"{env.erp_admin_url}/healthz"),
                          ("AI gateway admin", f"{env.aigw_admin_url}/healthz")):
            response = get(client, url)
            smoke.check(f"{name} answers on loopback", response is not None and response.status_code == 200)

        login = client.post(f"{env.front_doors['shop-lb']}/auth/demo-login", json={"sub": "alex.rivera"})
        if not smoke.check("alex.rivera logs in at shop-lb", login.status_code == 200, f"HTTP {login.status_code}"):
            return 1
        chat = client.post(f"{env.front_doors['shop-lb']}/v1/chat", timeout=120,
                           json={"messages": [{"role": "user", "content": "What is the status of my order?"}]},
                           headers={"Authorization": f"Bearer {login.json()['access_token']}"})
        if smoke.check("the shopper agent answers", chat.status_code == 200, f"HTTP {chat.status_code}"):
            trace = chat.json()["trace"]
            for kind, hop in (("token", "token exchange through idp-lb"), ("tool", "tool call through tools-lb"),
                              ("model", "model call through llm-lb")):
                steps = [s for s in trace if s["kind"] == kind]
                bad = [s for s in steps if s["status"] != 200 or s["blocked_by"]]
                if not steps:
                    detail = "not in the trace"
                elif bad:
                    detail = f"{bad[0]['status']} {bad[0].get('reason')}"
                else:
                    detail = ""
                smoke.check(hop, bool(steps) and not bad, detail)

        admin_via_lb = get(client, "http://127.0.0.1:8014/admin/state")
        smoke.check("ERP admin is not reachable through erp-lb",
                    admin_via_lb is not None and admin_via_lb.status_code in (401, 403),
                    "no response" if admin_via_lb is None else f"HTTP {admin_via_lb.status_code}")
    print("smoke: " + ("all ok" if not smoke.failures else f"{smoke.failures} failed"))
    return 1 if smoke.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", required=True, choices=["local", "tenant"])
    parser.add_argument("--no-security", action="store_true", help="tenant, Phase 2: plain LBs, no XC controls")
    args = parser.parse_args(argv)
    if args.env == "tenant":
        print("--env tenant is not built yet (Phase 2).", file=sys.stderr)
        return 2
    print("smoke, env local:")
    return run_local(demo_env.local())


if __name__ == "__main__":
    sys.exit(main())
