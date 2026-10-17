"""Fixtures for the MCP server tests.

The "world" fixture wires the REAL idp and the REAL erp-mock in-process (httpx.ASGITransport) behind the MCP
servers, so the tests exercise the same token and ERP contracts the containers use, with no network. The
erp_calls list records every request that reached the ERP, which lets tests prove a denied call never got there.
"""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from copperline_common.jwt import public_jwks, sign
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from erp_mock.app import create_admin_app, create_business_app
from erp_mock.db import Db
from erp_mock.settings import Settings as ErpSettings
from fastapi.testclient import TestClient
from idp.app import create_app as create_idp_app
from idp.settings import Settings as IdpSettings
from mcp_servers.app import Settings, create_app

REPO = Path(__file__).resolve().parents[3]
ISSUER = "https://idp.copperline.demo"
SVC_SECRET = "test-svc-secret"
IDP = "http://idp.test"
ERP = "http://erp.test"
POISONED = (REPO / "scenarios/data/poisoned-review.txt").read_text()
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def config_dir(tmp_path) -> Path:
    """The flat /config layout the containers see."""
    target = tmp_path / "config"
    target.mkdir()
    for source in ("config/tools/tools.yaml", "config/policy/tool-access-matrix.yaml", "config/personas.yaml"):
        shutil.copy(REPO / source, target)
    return target


@dataclass
class World:
    erp_admin: TestClient
    http: httpx.AsyncClient
    erp_calls: list = field(default_factory=list)
    idp_calls: list = field(default_factory=list)


class Recording(httpx.AsyncBaseTransport):
    """Routes idp.test to the IdP app and erp.test to the ERP app, recording each request."""

    def __init__(self, idp_app, erp_app, world: World, erp_down: bool = False) -> None:
        self.idp = httpx.ASGITransport(app=idp_app)
        self.erp = httpx.ASGITransport(app=erp_app)
        self.world = world
        self.erp_down = erp_down

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "idp.test":
            self.world.idp_calls.append(request)
            return await self.idp.handle_async_request(request)
        self.world.erp_calls.append(request)
        if self.erp_down:
            raise httpx.ConnectError("connection refused")
        return await self.erp.handle_async_request(request)


@pytest.fixture
def make_world(tmp_path, config_dir, rsa_key):
    """make_world(erp_down=False) -> World; one fresh ERP database per world."""
    pem = rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps(public_jwks(rsa_key)))
    clients = []

    def _make(erp_down: bool = False) -> World:
        idp_app = create_idp_app(IdpSettings(config_dir=config_dir, issuer=ISSUER, signing_key_pem=pem,
                                             client_secrets={"svc-mcp": SVC_SECRET}))
        erp_settings = ErpSettings(db_path=tmp_path / f"erp-{len(clients)}.sqlite3", issuer=ISSUER,
                                   jwks_path=str(jwks_path))
        db = Db(erp_settings.db_path, erp_settings.seed_path)
        world = World(erp_admin=TestClient(create_admin_app(erp_settings, db)), http=None)
        world.http = httpx.AsyncClient(transport=Recording(idp_app, create_business_app(erp_settings, db), world,
                                                           erp_down))
        clients.append((world, db))
        return world

    yield _make
    for world, db in clients:
        world.erp_admin.close()
        db.close()


@pytest.fixture
def server(config_dir, make_world):
    """server(name, erp_down=False) -> (TestClient for that MCP server, World)."""
    opened = []

    def _server(name: str, erp_down: bool = False):
        world = make_world(erp_down=erp_down)
        settings = Settings(server=name, config_dir=config_dir, erp_base_url=ERP, idp_base_url=IDP,
                            svc_client_secret=SVC_SECRET)
        client = TestClient(create_app(settings, http=world.http))
        opened.append(client)
        return client, world

    yield _server
    for client in opened:
        client.close()


@pytest.fixture
def agent_token(rsa_key):
    """agent_token(agent_role, obo_role, obo_sub=...) -> a token shaped like the IdP's agent tokens."""

    def _make(agent_role: str, obo_role: str, obo_sub: str = "someone") -> str:
        claims = {"sub": agent_role, "agent_id": agent_role, "agent_role": agent_role, "obo_sub": obo_sub,
                  "obo_role": obo_role, "aud": "copperline-tools"}
        return sign(claims, rsa_key, ttl_s=300, issuer=ISSUER)

    return _make


class Mcp:
    """Test helpers, handed out by the mcp fixture (tests can't import conftest under --import-mode=importlib)."""

    poisoned = POISONED

    @staticmethod
    def call(client: TestClient, server: str, tool: str, args: dict, token: str | None, *,
             header_name: str | None = None):
        """POST a tools/call the way McpClient does. header_name sends a different Mcp-Name (S1b)."""
        headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": header_name or tool}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": "1", "method": "tools/call",
                "params": {"name": tool, "arguments": args, "_meta": META}}
        return client.post(f"/mcp/{server}", json=body, headers=headers)

    @staticmethod
    def rpc(client: TestClient, server: str, method: str, token: str | None):
        """POST a tools/list or server/discover."""
        headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": "1", "method": method, "params": {"_meta": META}}
        return client.post(f"/mcp/{server}", json=body, headers=headers)

    @staticmethod
    def result(response) -> dict:
        """structuredContent of a successful tools/call, asserting it really succeeded."""
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["isError"] is False, result
        return result["structuredContent"]

    @staticmethod
    def tool_error(response) -> str:
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["isError"] is True, result
        return result["content"][0]["text"]


@pytest.fixture
def mcp() -> Mcp:
    return Mcp()
