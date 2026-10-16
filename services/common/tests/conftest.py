"""Shared fixtures for the copperline_common tests. No network: outbound HTTP uses httpx.MockTransport."""

import json
from pathlib import Path

import pytest
from copperline_common import jwt as cjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    assert (REPO_ROOT / "config/policy/tool-access-matrix.yaml").is_file()
    return REPO_ROOT


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_pem(rsa_key) -> bytes:
    return rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture
def make_token(rsa_key):
    """make_token(**claims) -> a signed RS256 token (5 minute lifetime)."""

    def _make(**claims) -> str:
        return cjwt.sign(claims, rsa_key, ttl_s=300, issuer="https://idp.test")

    return _make


def _json_lines(text: str) -> list[dict]:
    lines = []
    for line in text.splitlines():
        try:
            lines.append(json.loads(line))
        except ValueError:
            continue
    return lines


@pytest.fixture
def read_logs(capsys):
    """read_logs() -> JSON log records written to stdout since the last call."""

    def _read() -> list[dict]:
        return _json_lines(capsys.readouterr().out)

    return _read
