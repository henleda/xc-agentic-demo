"""Where each demo endpoint is, per environment, for scripts/run_scenario.py and scripts/smoke_test.py.

local   the compose stack (deploy/local): front doors and internal LBs on 127.0.0.1, admin ports on loopback.
tenant  the XC tenant: public front doors on <DEMO_DOMAIN>; internal LBs are NOT reachable from outside, so tenant
        runs go through the cockpit (Phase 2+). Only the front-door URLs are defined here for now.

Secrets (client secrets for S6's token step, the S5 job's copied credential) come from .secrets/secrets.env, read by
read_secrets(); nothing here prints them.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import read_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = ROOT / ".secrets" / "secrets.env"
CHAT_PATH = {"shopper": "/v1/chat", "storeops": "/storeops/v1/chat", "refunds": "/refunds/v1/chat"}


@dataclass
class DemoEnv:
    name: str
    front_doors: dict[str, str]  # shop-lb / ops-lb / partner-lb -> base URL
    tools_base_url: str | None = None  # internal LBs: only reachable directly in local mode
    llm_base_url: str | None = None
    idp_base_url: str | None = None
    erp_admin_url: str | None = None
    aigw_admin_url: str | None = None
    plan_env: dict[str, str] = field(default_factory=dict)  # ${VAR} values for plans run by this script

    def front_door_for(self, role: str) -> str:
        return "shop-lb" if role == "customer" else "ops-lb"


def local() -> DemoEnv:
    tools, llm, idp = "http://127.0.0.1:8011", "http://127.0.0.1:8012", "http://127.0.0.1:8013"
    partner = "http://127.0.0.1:8003"
    return DemoEnv(
        name="local",
        front_doors={"shop-lb": "http://127.0.0.1:8001", "ops-lb": "http://127.0.0.1:8002", "partner-lb": partner},
        tools_base_url=tools,
        llm_base_url=llm,
        idp_base_url=idp,
        erp_admin_url="http://127.0.0.1:8181",
        aigw_admin_url="http://127.0.0.1:8190",
        # S6's plan runs here (the cockpit's role); S5's EXFIL_TEST_URL is set inside the compose job itself.
        plan_env={"TOOLS_BASE_URL": tools, "LLM_BASE_URL": llm, "IDP_BASE_URL": idp, "PARTNER_BASE_URL": partner},
    )


def tenant() -> DemoEnv:
    domain = os.environ.get("DEMO_DOMAIN") or read_env_file(ROOT / ".env").get("DEMO_DOMAIN", "")
    if not domain:
        raise SystemExit("--env tenant needs DEMO_DOMAIN (in .env or the shell)")
    partner = f"https://partner.{domain}"
    return DemoEnv(
        name="tenant",
        front_doors={"shop-lb": f"https://shop.{domain}", "ops-lb": f"https://ops.{domain}", "partner-lb": partner},
        plan_env={"PARTNER_BASE_URL": partner},
    )


def for_name(name: str) -> DemoEnv:
    if name == "local":
        return local()
    if name == "tenant":
        return tenant()
    raise SystemExit(f"--env must be local or tenant, got {name!r}")


def read_secrets() -> dict[str, str]:
    """NAME -> value from .secrets/secrets.env (empty if gen_keys.py has not run). Never printed."""
    return read_env_file(SECRETS_FILE)
