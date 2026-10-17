"""Settings.from_env and startup: the IdP refuses to start without a usable signing key or config."""

import json
import shutil
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from idp.app import create_app
from idp.settings import DEFAULT_ISSUER, Settings

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
ENV_NAMES = ("CONFIG_DIR", "ISSUER", "SIGNING_KEY_PEM", "SIGNING_KEY_PATH", "CLIENT_SECRETS_JSON")


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def flat_config(tmp_path) -> Path:
    shutil.copy(REPO_CONFIG / "personas.yaml", tmp_path / "personas.yaml")
    shutil.copy(REPO_CONFIG / "policy" / "tool-access-matrix.yaml", tmp_path / "tool-access-matrix.yaml")
    return tmp_path


def test_from_env_reads_every_value(clean_env, pem, flat_config):
    clean_env.setenv("CONFIG_DIR", str(flat_config))
    clean_env.setenv("ISSUER", "https://idp.example")
    clean_env.setenv("SIGNING_KEY_PEM", pem)
    clean_env.setenv("CLIENT_SECRETS_JSON", json.dumps({"agent-shopper": "s1"}))
    settings = Settings.from_env()
    assert settings.config_dir == flat_config
    assert settings.issuer == "https://idp.example"
    assert settings.signing_key_pem == pem
    assert settings.client_secrets == {"agent-shopper": "s1"}


def test_from_env_defaults(clean_env, pem):
    clean_env.setenv("SIGNING_KEY_PEM", pem)
    settings = Settings.from_env()
    assert settings.issuer == DEFAULT_ISSUER == "https://idp.copperline.demo"
    assert settings.config_dir == Path("/config")
    assert settings.client_secrets is None  # per-client env vars are used instead


def test_signing_key_path_is_used_when_pem_is_unset(clean_env, pem, tmp_path):
    key_file = tmp_path / "key.pem"
    key_file.write_text(pem)
    clean_env.setenv("SIGNING_KEY_PATH", str(key_file))
    assert Settings.from_env().signing_key_pem == pem


def test_signing_key_pem_wins_over_path(clean_env, pem, tmp_path):
    clean_env.setenv("SIGNING_KEY_PEM", pem)
    clean_env.setenv("SIGNING_KEY_PATH", str(tmp_path / "does-not-exist.pem"))
    assert Settings.from_env().signing_key_pem == pem


@pytest.mark.parametrize("case", ["nothing", "missing-file"])
def test_no_signing_key_refuses_to_start(clean_env, tmp_path, case):
    if case == "missing-file":
        clean_env.setenv("SIGNING_KEY_PATH", str(tmp_path / "nope.pem"))
    with pytest.raises(RuntimeError, match="SIGNING_KEY"):
        Settings.from_env()


@pytest.mark.parametrize("raw", ["{not json", '["a"]', '{"agent-shopper": 5}'])
def test_bad_client_secrets_json_refuses_to_start_without_echoing_it(clean_env, pem, raw):
    clean_env.setenv("SIGNING_KEY_PEM", pem)
    clean_env.setenv("CLIENT_SECRETS_JSON", raw)
    with pytest.raises(RuntimeError, match="CLIENT_SECRETS_JSON") as info:
        Settings.from_env()
    assert raw not in str(info.value)


def test_garbage_pem_refuses_to_start(flat_config):
    garbage = "-----BEGIN NOTHING-----"
    settings = Settings(config_dir=flat_config, issuer="x", signing_key_pem=garbage, client_secrets={})
    with pytest.raises(ValueError):
        create_app(settings)


def test_app_without_settings_loads_env_at_startup(clean_env, pem, flat_config):
    clean_env.setenv("CONFIG_DIR", str(flat_config))
    clean_env.setenv("SIGNING_KEY_PEM", pem)
    clean_env.setenv("CLIENT_SECRETS_JSON", json.dumps({"svc-mcp": "svc-secret"}))
    form = {"grant_type": "client_credentials", "client_id": "svc-mcp", "client_secret": "svc-secret",
            "audience": "copperline-erp"}
    with TestClient(create_app()) as client:
        assert client.post("/auth/token", data=form).status_code == 200


def test_app_without_settings_fails_startup_without_a_key(clean_env, flat_config):
    clean_env.setenv("CONFIG_DIR", str(flat_config))
    with pytest.raises(RuntimeError, match="SIGNING_KEY"), TestClient(create_app()):
        pass
