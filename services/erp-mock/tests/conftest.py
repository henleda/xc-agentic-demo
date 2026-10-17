"""Shared fixtures for the erp-mock tests: a test RSA key, its JWKS file, a fresh database per test, both apps.

Nothing reads .secrets/ and nothing listens on a port (except test_impl_erp_settings_main.py's one server test).
"""

import json
from pathlib import Path

import pytest
from copperline_common.jwt import public_jwks, sign
from cryptography.hazmat.primitives.asymmetric import rsa
from erp_mock.app import create_admin_app, create_business_app
from erp_mock.db import Db
from erp_mock.settings import Settings
from fastapi.testclient import TestClient

ISSUER = "https://idp.copperline.demo"
SERVICE_CLAIMS = {"sub": "mcp-servers", "svc_role": "tool-server", "aud": "copperline-erp"}  # the svc-mcp client


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_file(tmp_path, rsa_key) -> Path:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(public_jwks(rsa_key)))
    return path


@pytest.fixture
def settings(tmp_path, jwks_file) -> Settings:
    return Settings(db_path=tmp_path / "erp.sqlite3", issuer=ISSUER, jwks_path=str(jwks_file))


@pytest.fixture
def db(settings):
    database = Db(settings.db_path, settings.seed_path)
    yield database
    database.close()


@pytest.fixture
def make_token(rsa_key):
    """make_token(**claims) -> RS256 token from the test key. Defaults to a valid svc-mcp service token."""

    def _make(key=None, issuer: str = ISSUER, ttl_s: int = 300, **claims) -> str:
        return sign(claims or dict(SERVICE_CLAIMS), key or rsa_key, ttl_s=ttl_s, issuer=issuer)

    return _make


@pytest.fixture
def auth(make_token) -> dict:
    return {"Authorization": f"Bearer {make_token()}"}


@pytest.fixture
def business(settings, db):
    with TestClient(create_business_app(settings, db)) as client:
        yield client


@pytest.fixture
def admin(settings, db):
    with TestClient(create_admin_app(settings, db)) as client:
        yield client


@pytest.fixture
def read_logs(capsys):
    """read_logs() -> (JSON log records, raw text) written to stdout since the last call."""

    def _read() -> tuple[list[dict], str]:
        text = capsys.readouterr().out
        records = []
        for line in text.splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records, text

    return _read
