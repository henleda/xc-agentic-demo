"""Settings.from_env, the shared database, and `python -m erp_mock` (both servers in one process, one database).

The server test is the only one that opens sockets: 127.0.0.1 on two free ports.
"""

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
from copperline_common.jwt import public_jwks, sign
from erp_mock import __main__ as erp_main
from erp_mock.app import create_admin_app, create_business_app
from erp_mock.db import Db, shared_db
from erp_mock.settings import DEFAULT_DB_PATH, DEFAULT_ISSUER, SEED_SQL, Settings
from fastapi.testclient import TestClient

ENV_NAMES = ("ERP_DB_PATH", "JWKS_PATH", "IDP_BASE_URL", "ISSUER", "ERP_PORT", "ERP_ADMIN_PORT")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---- settings -----------------------------------------------------------------------------------------------------


def test_from_env_defaults(clean_env):
    clean_env.setenv("JWKS_PATH", "/jwks/jwks.json")
    settings = Settings.from_env()
    assert settings.db_path == Path(DEFAULT_DB_PATH) == Path("/tmp/erp.sqlite3")
    assert settings.issuer == DEFAULT_ISSUER == "https://idp.copperline.demo"
    assert (settings.port, settings.admin_port, settings.host) == (8081, 8181, "0.0.0.0")
    assert settings.seed_path == SEED_SQL and SEED_SQL.is_file()
    assert settings.jwks_source() == "/jwks/jwks.json"


def test_from_env_reads_every_value(clean_env, tmp_path):
    clean_env.setenv("ERP_DB_PATH", str(tmp_path / "x.sqlite3"))
    clean_env.setenv("ISSUER", "https://idp.example")
    clean_env.setenv("IDP_BASE_URL", "http://idp.agentic-demo:8080/")
    clean_env.setenv("ERP_PORT", "9081")
    clean_env.setenv("ERP_ADMIN_PORT", "9181")
    settings = Settings.from_env()
    assert settings.db_path == tmp_path / "x.sqlite3"
    assert settings.issuer == "https://idp.example"
    assert (settings.port, settings.admin_port) == (9081, 9181)
    assert settings.jwks_source() == "http://idp.agentic-demo:8080/.well-known/jwks.json"


def test_jwks_path_wins_over_idp_url(clean_env):
    clean_env.setenv("JWKS_PATH", "/jwks/jwks.json")
    clean_env.setenv("IDP_BASE_URL", "http://idp.agentic-demo:8080")
    assert Settings.from_env().jwks_source() == "/jwks/jwks.json"


def test_no_key_source_refuses_to_start(clean_env):
    with pytest.raises(RuntimeError, match="JWKS_PATH"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["abc", "0", "70000", "-1"])
def test_bad_port_refuses_to_start(clean_env, value):
    clean_env.setenv("JWKS_PATH", "/jwks/jwks.json")
    clean_env.setenv("ERP_PORT", value)
    with pytest.raises(RuntimeError, match="ERP_PORT"):
        Settings.from_env()


def test_keys_are_fetched_from_the_idp_when_no_file(settings, rsa_key, monkeypatch):
    """IDP_BASE_URL mode: the verifier GETs ${IDP_BASE_URL}/.well-known/jwks.json (here a fake httpx.get)."""
    fetched = []

    def fake_get(url, timeout):
        fetched.append(url)
        return httpx.Response(200, json=public_jwks(rsa_key), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    url_settings = Settings(db_path=settings.db_path, issuer=settings.issuer, idp_base_url="http://idp.test")
    db = Db(url_settings.db_path)
    token = sign({"sub": "mcp-servers", "svc_role": "tool-server", "aud": "copperline-erp"}, rsa_key, ttl_s=60,
                 issuer=settings.issuer)
    with TestClient(create_business_app(url_settings, db)) as client:
        assert client.get("/orders/CO-1042", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    db.close()
    assert fetched == ["http://idp.test/.well-known/jwks.json"]


# ---- one database for both apps -----------------------------------------------------------------------------------


def test_apps_built_from_settings_share_one_database(settings, make_token):
    business_app, admin_app = create_business_app(settings), create_admin_app(settings)
    assert business_app.state.db is admin_app.state.db
    auth = {"Authorization": f"Bearer {make_token()}"}
    with TestClient(business_app) as business, TestClient(admin_app) as admin:
        assert business.post("/refunds", headers=auth, json={"order_id": "CO-1042", "amount": 1}).status_code == 201
        assert admin.get("/admin/state").json()["refunds_issued"] == 1


def test_a_restart_rebuilds_the_database_from_seed(settings, make_token):
    """The /tmp emptyDir survives a container restart; the estate must not."""
    auth = {"Authorization": f"Bearer {make_token()}"}
    with TestClient(create_business_app(settings)) as business:
        business.post("/refunds", headers=auth, json={"order_id": "CO-1042", "amount": 1})
    with TestClient(create_admin_app(settings)) as admin:
        assert admin.get("/admin/state").json()["refunds_issued"] == 0


def test_db_over_a_leftover_file_starts_from_seed(tmp_path):
    path = tmp_path / "erp.sqlite3"
    path.write_bytes(b"not a database")
    db = Db(path)
    assert db.state()["refunds_issued"] == 0
    db.close()


def test_shared_db_is_one_per_path(tmp_path):
    a, b = shared_db(tmp_path / "a.sqlite3"), shared_db(tmp_path / "a.sqlite3")
    assert a is b and shared_db(tmp_path / "b.sqlite3") is not a


def test_reset_is_fast(db):
    started = time.monotonic()
    for _ in range(5):
        db.reset()
    assert (time.monotonic() - started) / 5 < 0.5  # README limit is 2 s; seed.sql builds in milliseconds


# ---- python -m erp_mock -------------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_main_serves_both_apps_on_one_database(tmp_path, jwks_file, rsa_key):
    settings = Settings(db_path=tmp_path / "erp.sqlite3", issuer=DEFAULT_ISSUER, jwks_path=str(jwks_file),
                        port=free_port(), admin_port=free_port(), host="127.0.0.1")
    business, admin = erp_main.build_servers(settings)
    thread = threading.Thread(target=lambda: asyncio.run(erp_main.serve(business, admin)), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not (business.started and admin.started):
            assert time.monotonic() < deadline, "servers did not start"
            time.sleep(0.05)
        token = sign({"sub": "mcp-servers", "svc_role": "tool-server", "aud": "copperline-erp"}, rsa_key, ttl_s=60,
                     issuer=DEFAULT_ISSUER)
        biz_url, admin_url = f"http://127.0.0.1:{settings.port}", f"http://127.0.0.1:{settings.admin_port}"
        with httpx.Client(timeout=5) as http:
            assert http.get(f"{biz_url}/healthz").json() == {"status": "ok", "service": "erp-mock"}
            assert http.get(f"{admin_url}/healthz").json() == {"status": "ok", "service": "erp-mock-admin"}
            response = http.post(f"{biz_url}/refunds", json={"order_id": "CO-1042", "amount": 120},
                                 headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 201, response.text
            assert http.get(f"{admin_url}/admin/state").json()["refunds_issued"] == 1
            assert http.get(f"{biz_url}/admin/state").status_code == 401  # the admin API is not on the business port
            assert http.post(f"{admin_url}/admin/reset").json()["status"] == "reset"
            assert json.loads(http.get(f"{admin_url}/admin/state").text)["refunds_issued"] == 0
    finally:
        business.should_exit = admin.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()
