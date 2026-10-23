"""cockpit settings, read once from env at startup. Tests build a Settings directly.

The cockpit drives the demo from where it runs: locally on the laptop or devbox host (the compose stack's published
127.0.0.1 ports), on EC2 copperline-core in the tenant (the public front doors, and the admin ports on its loopback).

Files: from a checkout (make cockpit) scenarios/ and config/personas.yaml are found in the repo; in the image they are
baked in at /app/scenarios and /app/config/personas.yaml (SCENARIOS_DIR, PERSONAS_PATH set by the Dockerfile).
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from copperline_common.config import repo_root
from copperline_common.credentials import secret_env_name
from copperline_common.demo_driver import Endpoints

LOCAL = {
    "front_doors": {"shop-lb": "http://127.0.0.1:8001", "ops-lb": "http://127.0.0.1:8002",
                    "partner-lb": "http://127.0.0.1:8003"},
    "tools_base_url": "http://127.0.0.1:8011",
    "llm_base_url": "http://127.0.0.1:8012",
    "idp_base_url": "http://127.0.0.1:8013",
}


@dataclass
class Settings:
    env: str  # "local" or "tenant"
    endpoints: Endpoints
    scenarios_dir: Path
    personas_path: Path
    job_mode: str = "compose"  # how S5 starts: "compose" (local) or "vk8s" (tenant, Phase 2)
    compose_script: Path | None = None  # deploy/local/compose.sh, for job_mode compose
    console_links: dict[str, str] = field(default_factory=dict)  # scenario id -> XC console URL
    run_salt: str | None = None  # initial salt; Reset makes a new one
    port: int = 8095
    host: str = "127.0.0.1"  # local: loopback only (cockpit-lb is not emulated locally)

    @classmethod
    def from_env(cls) -> "Settings":
        """Raises RuntimeError on a value the cockpit cannot start with."""
        env = os.environ.get("COCKPIT_ENV") or "local"
        root = repo_root()  # None inside the image
        scenarios_dir = Path(os.environ.get("SCENARIOS_DIR") or (root / "scenarios" if root else "/app/scenarios"))
        personas_path = Path(os.environ.get("PERSONAS_PATH")
                             or (root / "config/personas.yaml" if root else "/app/config/personas.yaml"))
        partner_names = _partner_secret_names(personas_path)
        if env == "local":
            if root is None:
                raise RuntimeError("COCKPIT_ENV=local runs from a checkout (make cockpit); in the image set "
                                   "COCKPIT_ENV=tenant")
            endpoints = Endpoints(name="local", erp_admin_url="http://127.0.0.1:8181",
                                  aigw_admin_url="http://127.0.0.1:8190",
                                  plan_env={"PARTNER_BASE_URL": LOCAL["front_doors"]["partner-lb"]},
                                  secrets=_secrets(root, partner_names), **LOCAL)
        elif env == "tenant":
            domain = os.environ.get("DEMO_DOMAIN", "")
            if not domain:
                raise RuntimeError("COCKPIT_ENV=tenant needs DEMO_DOMAIN")
            partner = f"https://partner.{domain}"
            # On EC2 the partner's client secret comes from the env (.env: GRANITE_SUPPLY_SECRET); the cockpit refuses
            # to start without it rather than show an S6 ERROR on stage.
            secrets_from_env = {name: os.environ[name] for name in partner_names if os.environ.get(name)}
            missing = sorted(partner_names - set(secrets_from_env))
            if missing:
                raise RuntimeError(f"COCKPIT_ENV=tenant needs {', '.join(missing)} (S6) in the env")
            endpoints = Endpoints(name="tenant", front_doors={"shop-lb": f"https://shop.{domain}",
                                                              "ops-lb": f"https://ops.{domain}",
                                                              "partner-lb": partner},
                                  erp_admin_url="http://127.0.0.1:8181", aigw_admin_url="http://127.0.0.1:8190",
                                  plan_env={"PARTNER_BASE_URL": partner}, secrets=secrets_from_env)
        else:
            raise RuntimeError(f"COCKPIT_ENV must be local or tenant, got {env!r}")
        try:
            links = json.loads(os.environ.get("XC_CONSOLE_LINKS") or "{}")
        except ValueError:
            raise RuntimeError("XC_CONSOLE_LINKS must be a JSON object: {\"S1\": \"https://...\"}") from None
        return cls(
            env=env,
            endpoints=endpoints,
            scenarios_dir=scenarios_dir,
            personas_path=personas_path,
            job_mode="compose" if env == "local" else "vk8s",
            compose_script=root / "deploy/local/compose.sh" if root else None,
            console_links=links if isinstance(links, dict) else {},
            run_salt=os.environ.get("RUN_SALT") or None,
            port=int(os.environ.get("PORT") or 8095),
            host=os.environ.get("COCKPIT_HOST") or ("127.0.0.1" if env == "local" else "0.0.0.0"),
        )


def _partner_secret_names(personas_path: Path) -> set[str]:
    """GRANITE_SUPPLY_SECRET and the like: the only secrets the cockpit holds (S6 plays the partner)."""
    from copperline_common.personas import Personas

    return {secret_env_name(c["client_id"]) for c in Personas.load(personas_path).clients if c.get("kind") == "partner"}


def _secrets(root: Path, names: set[str]) -> dict[str, str]:
    """Locally: those names only, from .secrets/secrets.env (gen_keys.py writes plain unquoted NAME=value lines)."""
    path = root / ".secrets" / "secrets.env"
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            if name.strip() in names:
                values[name.strip()] = value.strip()
    return values
