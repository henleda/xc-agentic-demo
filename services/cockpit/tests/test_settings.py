"""cockpit Settings.from_env: what `make cockpit` and the EC2 image actually start with."""

from pathlib import Path

import pytest
from cockpit import settings as settings_module
from cockpit.settings import Settings

ENV_NAMES = ("COCKPIT_ENV", "DEMO_DOMAIN", "XC_CONSOLE_LINKS", "RUN_SALT", "PORT", "COCKPIT_HOST", "SCENARIOS_DIR",
             "PERSONAS_PATH", "GRANITE_SUPPLY_SECRET")
REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A fake checkout: personas.yaml, one scenario, and a secrets.env holding a partner and a non-partner secret."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config/personas.yaml").write_text((REPO / "config/personas.yaml").read_text())
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "scenarios/S0-baseline.yaml").write_text((REPO / "scenarios/S0-baseline.yaml").read_text())
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets/secrets.env").write_text("# test\nGRANITE_SUPPLY_SECRET=partner-s\nSVC_MCP_SECRET=svc-s\n"
                                                   "GATEWAY_API_KEY=gw-s\n")
    monkeypatch.setattr(settings_module, "repo_root", lambda: tmp_path)
    return tmp_path


def test_local_binds_loopback_and_holds_only_the_partner_secret(checkout):
    s = Settings.from_env()
    assert (s.env, s.host, s.port, s.job_mode) == ("local", "127.0.0.1", 8095, "compose")
    assert s.endpoints.secrets == {"GRANITE_SUPPLY_SECRET": "partner-s"}  # never the gateway key or service secret
    assert s.scenarios_dir == checkout / "scenarios" and s.compose_script == checkout / "deploy/local/compose.sh"
    assert s.endpoints.front_doors["shop-lb"] == "http://127.0.0.1:8001"


def test_tenant_needs_its_domain_and_the_partner_secret(checkout, monkeypatch):
    monkeypatch.setenv("COCKPIT_ENV", "tenant")
    with pytest.raises(RuntimeError, match="DEMO_DOMAIN"):
        Settings.from_env()
    monkeypatch.setenv("DEMO_DOMAIN", "demo.example")
    with pytest.raises(RuntimeError, match="GRANITE_SUPPLY_SECRET"):
        Settings.from_env()
    monkeypatch.setenv("GRANITE_SUPPLY_SECRET", "from-env")
    s = Settings.from_env()
    assert (s.host, s.job_mode, s.endpoints.front_doors["partner-lb"]) == ("0.0.0.0", "vk8s",
                                                                              "https://partner.demo.example")
    assert s.endpoints.secrets == {"GRANITE_SUPPLY_SECRET": "from-env"}


def test_in_the_image_the_baked_in_paths_are_used(monkeypatch, tmp_path):
    """No checkout (repo_root() is None): SCENARIOS_DIR / PERSONAS_PATH, which the Dockerfile sets."""
    monkeypatch.setattr(settings_module, "repo_root", lambda: None)
    monkeypatch.setenv("COCKPIT_ENV", "tenant")
    monkeypatch.setenv("DEMO_DOMAIN", "demo.example")
    monkeypatch.setenv("GRANITE_SUPPLY_SECRET", "s")
    monkeypatch.setenv("PERSONAS_PATH", str(REPO / "config/personas.yaml"))
    monkeypatch.setenv("SCENARIOS_DIR", "/app/scenarios")
    s = Settings.from_env()
    assert s.scenarios_dir == Path("/app/scenarios") and s.compose_script is None
    dockerfile = (REPO / "services/cockpit/Dockerfile").read_text()
    assert "SCENARIOS_DIR=/app/scenarios" in dockerfile and "PERSONAS_PATH=/app/config/personas.yaml" in dockerfile
    assert "COPY scenarios/ /app/scenarios/" in dockerfile and "USER 10001" in dockerfile


def test_local_mode_needs_a_checkout(monkeypatch):
    monkeypatch.setattr(settings_module, "repo_root", lambda: None)
    monkeypatch.setenv("PERSONAS_PATH", str(REPO / "config/personas.yaml"))
    with pytest.raises(RuntimeError, match="checkout"):
        Settings.from_env()


def test_console_links(checkout, monkeypatch):
    monkeypatch.setenv("XC_CONSOLE_LINKS", '{"S1": "https://console.example/s1"}')
    assert Settings.from_env().console_links == {"S1": "https://console.example/s1"}
    monkeypatch.setenv("XC_CONSOLE_LINKS", "{not json")
    with pytest.raises(RuntimeError, match="XC_CONSOLE_LINKS"):
        Settings.from_env()
    monkeypatch.setenv("XC_CONSOLE_LINKS", '["a list"]')
    assert Settings.from_env().console_links == {}


def test_an_unknown_env_is_refused(checkout, monkeypatch):
    monkeypatch.setenv("COCKPIT_ENV", "staging")
    with pytest.raises(RuntimeError, match="COCKPIT_ENV"):
        Settings.from_env()


def test_the_ec2_compose_runs_the_cockpit_in_tenant_mode():
    import yaml

    service = yaml.safe_load((REPO / "deploy/aws/core/docker-compose.yaml").read_text())["services"]["cockpit"]
    assert service["environment"]["COCKPIT_ENV"] == "tenant" and service["network_mode"] == "host"
