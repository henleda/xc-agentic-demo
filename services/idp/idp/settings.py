"""IdP settings, read once from env at startup. Tests build a Settings directly and pass it to create_app()."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from copperline_common import config as common_config

DEFAULT_ISSUER = "https://idp.copperline.demo"


@dataclass
class Settings:
    config_dir: Path  # holds personas.yaml and tool-access-matrix.yaml (the flat ConfigMap mount)
    issuer: str
    signing_key_pem: str
    # client_id -> secret, from CLIENT_SECRETS_JSON. None means "not set": each secret then comes from
    # credentials.client_secret(client_id), i.e. env AGENT_SHOPPER_SECRET and so on.
    client_secrets: dict[str, str] | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        """Raises RuntimeError when the signing key or CLIENT_SECRETS_JSON is unusable: the IdP must not start."""
        return cls(
            config_dir=common_config.config_dir(),
            issuer=os.environ.get("ISSUER") or DEFAULT_ISSUER,
            signing_key_pem=signing_key_pem_from_env(),
            client_secrets=client_secrets_from_env(),
        )


def signing_key_pem_from_env() -> str:
    """SIGNING_KEY_PEM (the PEM text), or else the file at SIGNING_KEY_PATH."""
    pem = os.environ.get("SIGNING_KEY_PEM", "")
    if pem.strip():
        return pem
    path = os.environ.get("SIGNING_KEY_PATH", "")
    if not path:
        raise RuntimeError("no signing key: set SIGNING_KEY_PEM or SIGNING_KEY_PATH (see scripts/gen_keys.py)")
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"cannot read SIGNING_KEY_PATH {path}: {e.strerror}") from None


def client_secrets_from_env() -> dict[str, str] | None:
    """CLIENT_SECRETS_JSON as a dict, or None when it is unset or empty. Never echoes the value in errors."""
    raw = os.environ.get("CLIENT_SECRETS_JSON", "")
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError("CLIENT_SECRETS_JSON is not valid JSON") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise RuntimeError('CLIENT_SECRETS_JSON must be a JSON object of strings: {"agent-shopper": "...", ...}')
    return data
