#!/usr/bin/env python3
"""Print the few non-secret .env settings local compose uses, as NAME=value lines, for compose.sh to export.

docker compose here reads only .secrets/secrets.env (--env-file), so values in the repo-root .env would never reach
it. This passes through just the names in PASSED: how ai-gw talks to Bedrock and its budgets. A variable already set
in the shell wins over .env. Nothing secret is read from .env here.

Refuses (exit 2) a half-configured Bedrock switch: LOCAL_STUB_MODEL=false with no ROUTE_* set would otherwise make
ai-gw exit at startup with a less obvious message.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from envfile import read_env_file  # noqa: E402

PASSED = ("LOCAL_STUB_MODEL", "AWS_REGION", "ROUTE_FAST", "ROUTE_STANDARD", "ROUTE_PREMIUM",
          "BUDGET_TOKENS_FAST", "BUDGET_TOKENS_STANDARD", "BUDGET_TOKENS_PREMIUM")
ROUTES = ("ROUTE_FAST", "ROUTE_STANDARD", "ROUTE_PREMIUM")


def local_settings(env_file: Path, shell: dict[str, str]) -> dict[str, str]:
    from_file = read_env_file(env_file)
    values = {}
    for name in PASSED:
        value = shell.get(name) or from_file.get(name) or ""
        if value:
            values[name] = value
    return values


def check(values: dict[str, str]) -> str | None:
    stub = values.get("LOCAL_STUB_MODEL", "true").strip().lower()
    if stub not in ("true", "false"):
        return f"LOCAL_STUB_MODEL must be true or false, got {stub!r}"
    if stub == "false" and not any(values.get(name) for name in ROUTES):
        return ("LOCAL_STUB_MODEL=false needs at least one of ROUTE_FAST, ROUTE_STANDARD, ROUTE_PREMIUM "
                "(Bedrock model or inference-profile IDs) in .env or the shell")
    return None


def main() -> int:
    values = local_settings(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".env"), dict(os.environ))
    problem = check(values)
    if problem:
        print(f"local_env: {problem}", file=sys.stderr)
        return 2
    for name, value in values.items():
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
