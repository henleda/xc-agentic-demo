"""ai-gw settings, read once from env at startup. Tests build a Settings directly."""

import os
from dataclasses import dataclass, field

ROUTES = ("fast", "standard", "premium")
DEFAULT_PORT = 8090
DEFAULT_ADMIN_PORT = 8190
DEFAULT_BUDGET = 200_000


@dataclass
class Settings:
    stub_model: bool
    route_models: dict[str, str]  # route -> Bedrock model or inference-profile ID; empty in stub mode is fine
    budgets: dict[str, int]  # route -> tokens per UTC day
    api_key: str | None = None
    require_key: bool = True
    aws_region: str | None = None
    port: int = DEFAULT_PORT
    admin_port: int = DEFAULT_ADMIN_PORT
    host: str = "0.0.0.0"
    stub_replies_path: str | None = None  # default: ai_gw/stub_replies.yaml in the package
    max_tokens_cap: int = 2048  # a caller may ask for less, never more
    bedrock_timeout_s: float = 30.0
    unknown_routes: list[str] = field(default_factory=list)  # routes with no model ID (Bedrock mode only)

    @classmethod
    def from_env(cls) -> "Settings":
        """Raises RuntimeError on a value ai-gw cannot start with."""
        settings = cls(
            stub_model=env_bool("DEMO_STUB_MODEL", False),
            route_models={route: value for route in ROUTES if (value := os.environ.get(f"ROUTE_{route.upper()}", ""))},
            budgets={route: env_int(f"BUDGET_TOKENS_{route.upper()}", DEFAULT_BUDGET) for route in ROUTES},
            api_key=os.environ.get("GATEWAY_API_KEY") or None,
            require_key=env_bool("GATEWAY_REQUIRE_KEY", True),
            aws_region=os.environ.get("AWS_REGION") or None,
            port=env_int("AIGW_PORT", DEFAULT_PORT),
            admin_port=env_int("AIGW_ADMIN_PORT", DEFAULT_ADMIN_PORT),
            stub_replies_path=os.environ.get("STUB_REPLIES_PATH") or None,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.require_key and not self.api_key:
            raise RuntimeError("GATEWAY_REQUIRE_KEY is on but GATEWAY_API_KEY is not set")
        if not self.stub_model:
            if not self.aws_region:
                raise RuntimeError("Bedrock mode (DEMO_STUB_MODEL=false) needs AWS_REGION")
            if not self.route_models:
                raise RuntimeError("Bedrock mode needs at least one of ROUTE_FAST, ROUTE_STANDARD, ROUTE_PREMIUM")
        self.unknown_routes = [] if self.stub_model else [r for r in ROUTES if r not in self.route_models]


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"{name} must be true or false, got {raw!r}")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise RuntimeError(f"{name} must be a whole number, got {raw!r}")
    return int(raw)
