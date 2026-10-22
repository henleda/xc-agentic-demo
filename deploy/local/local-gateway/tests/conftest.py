"""Fixtures for the local-gateway tests.

`stack` wires the REAL idp, erp-mock and four MCP servers behind in-process local-gateways, one per XC load balancer,
on the same hostnames docker compose uses. Nothing listens on a socket: an httpx transport routes each hostname to
an ASGI app. So the tests exercise the same intent files, tokens, policies and MCP contracts as the containers.
"""

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from agent_runtime.app import build_agent
from agent_runtime.app import create_app as create_agent_app
from agent_runtime.settings import AgentConfig
from agent_runtime.settings import Settings as AgentSettings
from ai_gw.app import create_admin_app as create_aigw_admin_app
from ai_gw.app import create_gateway_app as create_aigw_app
from ai_gw.settings import Settings as AigwSettings
from ai_gw.usage import Usage
from copperline_common.jwt import public_jwks, sign
from copperline_common.outbound import new_http_client
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from erp_mock.app import create_admin_app as create_erp_admin_app
from erp_mock.app import create_business_app
from erp_mock.db import Db
from erp_mock.settings import Settings as ErpSettings
from idp.app import create_app as create_idp_app
from idp.settings import Settings as IdpSettings
from local_gateway.app import Settings as GatewaySettings
from local_gateway.app import create_app as create_gateway
from mcp_servers.app import Settings as McpSettings
from mcp_servers.app import create_app as create_mcp_app
from partner_api.app import create_app as create_partner_app
from partner_api.settings import Settings as PartnerSettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

REPO = Path(__file__).resolve().parents[4]
INTENT = REPO / "deploy/xc/intent"
POOLS = REPO / "deploy/local/pools.yaml"
ISSUER = "https://idp.copperline.demo"
GATEWAY_KEY = "test-gateway-key"
SECRETS = {
    "agent-shopper": "s-shopper",
    "agent-storeops": "s-storeops",
    "agent-refunds": "s-refunds",
    "svc-mcp": "s-svc",
    "granite-supply": "s-granite",
}
# Internal LBs (same hostnames as vK8s and compose) and the front doors (compose service names).
LB_HOSTS = {
    "tools.agentic-demo": "tools-lb",
    "llm.agentic-demo": "llm-lb",
    "idp.agentic-demo": "idp-lb",
    "erp.agentic-demo": "erp-lb",
    "shop-lb": "shop-lb",
    "ops-lb": "ops-lb",
    "partner-lb": "partner-lb",
}


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_path(tmp_path, rsa_key) -> Path:
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(public_jwks(rsa_key)))
    return path


@pytest.fixture
def token(rsa_key):
    """token(claims, ttl_s=300, key=None) -> an RS256 token from the test IdP key (or another key)."""

    def _make(claims: dict, ttl_s: int = 300, key=None) -> str:
        return sign(claims, key or rsa_key, ttl_s=ttl_s, issuer=ISSUER)

    return _make


@pytest.fixture
def config_dir(tmp_path) -> Path:
    """The flat /config layout the containers see."""
    target = tmp_path / "config"
    target.mkdir()
    for source in ("config/tools/tools.yaml", "config/policy/tool-access-matrix.yaml", "config/personas.yaml"):
        shutil.copy(REPO / source, target)
    return target


class Mesh(httpx.AsyncBaseTransport):
    """hostname -> ASGI app. An unknown hostname behaves like a refused connection."""

    def __init__(self) -> None:
        self.apps: dict[str, object] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        app = self.apps.get(request.url.host)
        if app is None:
            raise httpx.ConnectError(f"connection refused: {request.url.host}", request=request)
        return await httpx.ASGITransport(app=app).handle_async_request(request)


def gateway(lb_name: str, jwks_path: Path, http: httpx.AsyncClient, pools: Path = POOLS):
    settings = GatewaySettings(lb_name=lb_name, intent_dir=INTENT, pools_file=pools, jwks_path=jwks_path)
    return create_gateway(settings, http=http, env={"GATEWAY_API_KEY": GATEWAY_KEY})


async def echo(request: Request) -> JSONResponse:
    """A pool that answers with what it received, tagged like one of our services."""
    body = {
        "method": request.method,
        "path": request.scope["path"],  # not request.url: it splits a decoded "?" out of the path
        "query": request.scope["query_string"].decode(),
        "headers": [[name.decode("latin-1"), value.decode("latin-1")] for name, value in request.headers.raw],
        "headers_raw": [[name.decode("latin-1"), value.hex()] for name, value in request.headers.raw],  # exact bytes
        "body": (await request.body()).decode(),
    }
    return JSONResponse(body, status_code=int(request.headers.get("x-echo-status", "200")),
                        headers={"x-copperline-origin": "echo", "x-echo-extra": "kept"})


@pytest.fixture
def echo_gateway(tmp_path, jwks_path):
    """echo_gateway(lb_name, intent_dir=INTENT) -> an AsyncClient that talks to that LB, with every pool replaced by
    an echo app. Pass intent_dir to run the gateway on an edited copy of the intent files."""
    pools = tmp_path / "pools-echo.yaml"
    names = [line.split(":", 1)[0] for line in POOLS.read_text().splitlines() if line and not line.startswith("#")]
    pools.write_text("".join(f"{name}: http://echo:8080\n" for name in names))
    mesh = Mesh()
    mesh.apps["echo"] = Starlette(routes=[Route("/{path:path}", echo, methods=["GET", "POST", "PUT", "DELETE"])])
    upstream = httpx.AsyncClient(transport=mesh)

    def _client(lb_name: str, intent_dir: Path = INTENT) -> httpx.AsyncClient:
        settings = GatewaySettings(lb_name=lb_name, intent_dir=intent_dir, pools_file=pools, jwks_path=jwks_path)
        app = create_gateway(settings, http=upstream, env={"GATEWAY_API_KEY": GATEWAY_KEY})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lb")

    return _client


class Unreachable(httpx.AsyncBaseTransport):
    """Every pool fails the same way: a refused connection, or (timeout=True) a read timeout."""

    def __init__(self, timeout: bool = False) -> None:
        self.timeout = timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.timeout:
            raise httpx.ReadTimeout("pool too slow", request=request)
        raise httpx.ConnectError(f"connection refused: {request.url.host}", request=request)


@pytest.fixture
def unreachable_gateway(jwks_path):
    """unreachable_gateway(lb_name, timeout=False) -> an AsyncClient for that LB whose pools all fail."""

    def _client(lb_name: str, timeout: bool = False) -> httpx.AsyncClient:
        app = gateway(lb_name, jwks_path, httpx.AsyncClient(transport=Unreachable(timeout)))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lb")

    return _client


@dataclass
class Stack:
    http: httpx.AsyncClient  # a client "inside the compose network": it resolves every LB and pool hostname
    db: Db
    usage: Usage  # ai-gw's token usage (what GET /v1/usage on the admin port returns)
    mesh: "Mesh"  # hostname -> app; tests may swap one out to break a hop


@pytest.fixture
def stack(tmp_path, config_dir, rsa_key, jwks_path, monkeypatch):
    """The real services, agents and partner-api behind the real gateway logic; ai-gw in stub mode."""
    for client_id, secret in SECRETS.items():
        monkeypatch.setenv(client_id.upper().replace("-", "_") + "_SECRET", secret)  # plan token steps read env
    monkeypatch.setenv("EXFIL_TEST_URL", "https://exfil.invalid/rows")  # as compose sets it; plans read process env
    pem = rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    mesh = Mesh()
    http = new_http_client(transport=mesh)

    erp_settings = ErpSettings(db_path=tmp_path / "erp.sqlite3", issuer=ISSUER, jwks_path=str(jwks_path))
    db = Db(erp_settings.db_path, erp_settings.seed_path)
    mesh.apps["erp-mock"] = create_business_app(erp_settings, db)
    mesh.apps["erp-mock-admin"] = create_erp_admin_app(erp_settings, db)
    mesh.apps["idp"] = create_idp_app(IdpSettings(config_dir=config_dir, issuer=ISSUER, signing_key_pem=pem,
                                                  client_secrets=dict(SECRETS)))
    for server in ("catalog", "orders", "inventory", "refunds"):
        settings = McpSettings(server=server, config_dir=config_dir, erp_base_url="http://erp.agentic-demo",
                               idp_base_url="http://idp.agentic-demo", svc_client_secret=SECRETS["svc-mcp"])
        mesh.apps[f"mcp-{server}"] = create_mcp_app(settings, http=http)
    aigw_settings = AigwSettings(stub_model=True, route_models={}, api_key=GATEWAY_KEY,
                                 budgets={"fast": 200_000, "standard": 200_000, "premium": 50_000})
    usage = Usage(aigw_settings.budgets)
    mesh.apps["ai-gw"] = create_aigw_app(aigw_settings, usage)
    mesh.apps["ai-gw-admin"] = create_aigw_admin_app(aigw_settings, usage)
    for agent_id in ("shopper", "storeops", "refunds"):
        agent_settings = AgentSettings(
            agent=AgentConfig.load(REPO / f"config/agents/{agent_id}.yaml"), client_secret=SECRETS[f"agent-{agent_id}"],
            tools_base_url="http://tools.agentic-demo", llm_base_url="http://llm.agentic-demo",
            idp_base_url="http://idp.agentic-demo", config_dir=config_dir, scenarios_dir=REPO / "scenarios")
        agent = build_agent(agent_settings, http=http, model_http=http)
        mesh.apps[f"{agent_id}-agent"] = create_agent_app(agent_settings, agent)
    mesh.apps["partner-api"] = create_partner_app(PartnerSettings(issuer=ISSUER, jwks_path=str(jwks_path)))
    for host, lb_name in LB_HOSTS.items():
        mesh.apps[host] = gateway(lb_name, jwks_path, http)

    yield Stack(http=http, db=db, usage=usage, mesh=mesh)
    db.close()


# ---- the published local ports, served by the in-process stack (smoke and cockpit tests) --------------------------

# The published local ports -> the in-process hostnames the stack fixture serves.
PORT_HOST = {8001: "shop-lb", 8002: "ops-lb", 8003: "partner-lb", 8011: "tools.agentic-demo",
             8012: "llm.agentic-demo", 8013: "idp.agentic-demo", 8014: "erp.agentic-demo",
             8181: "erp-mock-admin", 8190: "ai-gw-admin"}
STACK_SECRETS = {"AGENT_SHOPPER_SECRET": "s-shopper", "AGENT_STOREOPS_SECRET": "s-storeops",
                 "AGENT_REFUNDS_SECRET": "s-refunds", "SVC_MCP_SECRET": "s-svc", "GRANITE_SUPPLY_SECRET": "s-granite"}


class SyncOverStack(httpx.BaseTransport):
    """smoke_test uses a sync httpx.Client; the stack is async ASGI. Route 127.0.0.1:<port> to the right app."""

    def __init__(self, stack) -> None:
        self.stack = stack

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url.copy_with(scheme="http", host=PORT_HOST[request.url.port], port=None)

        async def go():
            response = await self.stack.http.request(request.method, url, headers=request.headers,
                                                     content=request.read())
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        return asyncio.run(go())


@pytest.fixture
def local_ports(stack, monkeypatch):
    """Make sync httpx.Client calls to the published local ports (127.0.0.1:8001 ...) reach the in-process stack, as
    scripts and the cockpit backend do against compose. Returns the stack's client secrets (what secrets.env holds)."""
    real_client = httpx.Client

    def client_on_stack(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=SyncOverStack(stack), **kwargs)

    class AsyncOverStack(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            url = request.url.copy_with(scheme="http", host=PORT_HOST[request.url.port], port=None)
            response = await stack.http.request(request.method, url, headers=request.headers,
                                                content=await request.aread())
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    real_async = httpx.AsyncClient

    def async_on_stack(*args, **kwargs):
        if "transport" in kwargs:  # the stack's own clients pass a transport: leave them alone
            return real_async(*args, **kwargs)
        return real_async(transport=AsyncOverStack(), **kwargs)

    monkeypatch.setattr(httpx, "Client", client_on_stack)
    monkeypatch.setattr(httpx, "AsyncClient", async_on_stack)
    return dict(STACK_SECRETS)
