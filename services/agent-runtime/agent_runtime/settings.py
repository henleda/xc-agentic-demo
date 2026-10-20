"""agent-runtime settings: the agent's YAML (AGENT_CONFIG) plus env. Read once at startup; tests build them directly."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from copperline_common.config import load_yaml

DEFAULT_MAX_STEPS = 8
# Model calls wait longer than llm-lb does (locally 60 s; in XC see docs/08 R39), so a slow model ends as llm-lb's or
# ai-gw's own 504 in the trace, never as an agent-side timeout that hides who gave up.
DEFAULT_MODEL_TIMEOUT_S = 65.0
DEFAULT_TOOL_TIMEOUT_S = 10.0


@dataclass
class AgentConfig:
    """One config/agents/<agent>.yaml. Only the fields the runtime uses."""

    agent_id: str
    agent_role: str
    client_id: str
    model_route: str
    system_prompt: str
    declared_tools: list[str]
    max_steps: int = DEFAULT_MAX_STEPS
    mode: str = "server"

    @classmethod
    def load(cls, path: str | Path) -> "AgentConfig":
        data = load_yaml(path)
        if not isinstance(data, dict):
            raise RuntimeError(f"{path}: not a YAML mapping")
        missing = [key for key in ("agent_id", "agent_role", "client_id", "model_route") if not data.get(key)]
        if missing:
            raise RuntimeError(f"{path}: missing {', '.join(missing)}")
        return cls(
            agent_id=str(data["agent_id"]),
            agent_role=str(data["agent_role"]),
            client_id=str(data["client_id"]),
            model_route=str(data["model_route"]),
            system_prompt=str(data.get("system_prompt") or "").strip(),
            declared_tools=[str(tool) for tool in data.get("declared_tools") or []],
            max_steps=int(data.get("max_steps") or DEFAULT_MAX_STEPS),
            mode=str(data.get("mode") or "server"),
        )


@dataclass
class Settings:
    agent: AgentConfig
    client_secret: str | None
    tools_base_url: str
    llm_base_url: str
    idp_base_url: str
    config_dir: Path
    scenarios_dir: Path
    safe_mode: bool = True
    mode: str = "server"  # "server" or "batch"
    model_timeout_s: float = DEFAULT_MODEL_TIMEOUT_S
    tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    port: int = 8080
    extra_env: dict[str, str] = field(default_factory=dict)  # tests: env for plan ${VAR} substitution

    @classmethod
    def from_env(cls) -> "Settings":
        """Raises RuntimeError on a value the runtime cannot start with."""
        config_path = os.environ.get("AGENT_CONFIG", "")
        if not config_path:
            raise RuntimeError("set AGENT_CONFIG to the agent YAML, e.g. /config/shopper.yaml")
        agent = AgentConfig.load(config_path)
        mode = os.environ.get("MODE") or agent.mode
        if mode not in ("server", "batch"):
            raise RuntimeError(f"MODE must be server or batch, got {mode!r}")
        names = ("TOOLS_BASE_URL", "LLM_BASE_URL", "IDP_BASE_URL")
        urls = {name: os.environ.get(name, "").rstrip("/") for name in names}
        missing = [name for name, value in urls.items() if not value]
        if missing:
            raise RuntimeError(f"missing {', '.join(missing)}")
        secret = os.environ.get("AGENT_CLIENT_SECRET") or None
        if mode == "server" and not secret:
            raise RuntimeError("server mode needs AGENT_CLIENT_SECRET (the agent exchanges user tokens with it)")
        return cls(
            agent=agent,
            client_secret=secret,
            tools_base_url=urls["TOOLS_BASE_URL"],
            llm_base_url=urls["LLM_BASE_URL"],
            idp_base_url=urls["IDP_BASE_URL"],
            config_dir=Path(config_path).parent,
            scenarios_dir=Path(os.environ.get("SCENARIOS_DIR") or "/app/scenarios"),
            safe_mode=os.environ.get("DEMO_SAFE_MODE", "true").strip().lower() in ("1", "true", "yes", "on"),
            mode=mode,
            model_timeout_s=float(os.environ.get("MODEL_TIMEOUT_S") or DEFAULT_MODEL_TIMEOUT_S),
            port=int(os.environ.get("PORT") or 8080),
        )
