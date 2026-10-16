"""Client secrets come from env, one variable per client_id."""

import os
import re


def secret_env_name(client_id: str) -> str:
    """'agent-shopper' -> 'AGENT_SHOPPER_SECRET'."""
    return re.sub(r"[^A-Za-z0-9]", "_", client_id).upper() + "_SECRET"


def client_secret(client_id: str) -> str | None:
    return os.environ.get(secret_env_name(client_id))
