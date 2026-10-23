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
from pathlib import Path

from copperline_common.demo_driver import CHAT_PATH, Endpoints  # noqa: F401  (CHAT_PATH re-exported for tests)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import read_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = ROOT / ".secrets" / "secrets.env"


DemoEnv = Endpoints  # the shared shape (copperline_common.demo_driver); this module only fills it in


def local() -> DemoEnv:
    tools, llm, idp = "http://127.0.0.1:8011", "http://127.0.0.1:8012", "http://127.0.0.1:8013"
    partner = "http://127.0.0.1:8003"
    return DemoEnv(
        name="local",
        front_doors={"shop-lb": "http://127.0.0.1:8001", "ops-lb": "http://127.0.0.1:8002", "partner-lb": partner},
        tools_base_url=tools,
        llm_base_url=llm,
        idp_base_url=idp,
        erp_lb_url="http://127.0.0.1:8014",
        erp_admin_url="http://127.0.0.1:8181",
        aigw_admin_url="http://127.0.0.1:8190",
        # S6's plan runs here (the cockpit's role); S5's EXFIL_TEST_URL is set inside the compose job itself.
        plan_env={"TOOLS_BASE_URL": tools, "LLM_BASE_URL": llm, "IDP_BASE_URL": idp, "PARTNER_BASE_URL": partner},
        secrets=partner_secrets(),
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
        secrets=partner_secrets(),
    )


def job_env(env: DemoEnv) -> dict[str, str]:
    """What the S5 batch job's own environment holds, for --dry. The job's plan runs inside its container, so its
    variables come from where the container gets them, not from this shell:
      local   docker-compose.yaml's insights-nightly service (EXFIL_TEST_URL is set there)
      tenant  the runtime-env ConfigMap (deploy/vk8s/00-config.yaml) plus EXFIL_TEST_URL, which render_manifests.py
              takes from .env (deploy/vk8s/40-insights-cronjob.yaml). Missing from .env means missing in the pod."""
    import yaml

    if env.name == "local":
        compose = yaml.safe_load((ROOT / "deploy/local/docker-compose.yaml").read_text())
        service = compose["services"]["insights-nightly"]
        return {k: str(v) for k, v in service["environment"].items() if "${" not in str(v)}
    config_maps = {doc["metadata"]["name"]: doc["data"]
                   for doc in yaml.safe_load_all((ROOT / "deploy/vk8s/00-config.yaml").read_text()) if doc}
    values = dict(config_maps["runtime-env"])
    exfil = os.environ.get("EXFIL_TEST_URL") or read_env_file(ROOT / ".env").get("EXFIL_TEST_URL", "")
    if exfil:
        values["EXFIL_TEST_URL"] = exfil
    return values


def for_name(name: str) -> DemoEnv:
    if name == "local":
        return local()
    if name == "tenant":
        return tenant()
    raise SystemExit(f"--env must be local or tenant, got {name!r}")


def partner_secrets() -> dict[str, str]:
    """Only the partner clients' secrets (what S6 needs) from .secrets/secrets.env: an Endpoints never holds more."""
    from copperline_common.credentials import secret_env_name
    from copperline_common.personas import Personas

    clients = Personas.load(ROOT / "config/personas.yaml").clients
    names = {secret_env_name(c["client_id"]) for c in clients if c.get("kind") == "partner"}
    return {name: value for name, value in read_secrets().items() if name in names}


def read_secrets() -> dict[str, str]:
    """NAME -> value from .secrets/secrets.env (empty if gen_keys.py has not run). Never printed."""
    return read_env_file(SECRETS_FILE)
