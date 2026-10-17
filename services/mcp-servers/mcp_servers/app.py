"""Builds one MCP server app. MCP_SERVER picks which: catalog, orders, inventory or refunds."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from copperline_common.config import config_dir, load_yaml
from copperline_common.mcp import mcp_router
from copperline_common.outbound import new_http_client
from copperline_common.personas import Personas
from copperline_common.service import setup_service
from copperline_common.tool_schemas import load_tools, tools_for_server
from fastapi import FastAPI

from mcp_servers import tools as handlers
from mcp_servers.authz import Matrix, make_authorize
from mcp_servers.erp import ErpClient

SERVERS = ("catalog", "orders", "inventory", "refunds")
ERP_SERVERS = ("orders", "inventory", "refunds")
PACKAGED_CATALOG = Path(__file__).parent / "data" / "catalog.json"  # inside the wheel / image
SOURCE_CATALOG = Path(__file__).parent.parent / "data" / "catalog.json"  # in the repo checkout


@dataclass
class Settings:
    server: str
    config_dir: Path
    catalog_path: Path | None = None
    erp_base_url: str | None = None
    idp_base_url: str | None = None
    svc_client_id: str = "svc-mcp"
    svc_client_secret: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        catalog = os.environ.get("CATALOG_PATH")
        return cls(
            server=os.environ.get("MCP_SERVER", ""),
            config_dir=config_dir(),
            catalog_path=Path(catalog) if catalog else None,
            erp_base_url=os.environ.get("ERP_BASE_URL") or None,
            idp_base_url=os.environ.get("IDP_BASE_URL") or None,
            svc_client_id=os.environ.get("SVC_CLIENT_ID") or "svc-mcp",
            svc_client_secret=os.environ.get("SVC_CLIENT_SECRET") or None,
        )

    def validate(self) -> None:
        """Refuse to start half-configured (README "Process")."""
        if self.server not in SERVERS:
            raise RuntimeError(f"MCP_SERVER must be one of {', '.join(SERVERS)}, got {self.server!r}")
        if self.server in ERP_SERVERS:
            missing = [name for name, value in (("ERP_BASE_URL", self.erp_base_url),
                                                ("IDP_BASE_URL", self.idp_base_url),
                                                ("SVC_CLIENT_SECRET", self.svc_client_secret)) if not value]
            if missing:
                raise RuntimeError(f"mcp-{self.server} needs {', '.join(missing)}")


def default_catalog_path() -> Path:
    return PACKAGED_CATALOG if PACKAGED_CATALOG.is_file() else SOURCE_CATALOG


def create_app(settings: Settings, *, http: httpx.AsyncClient | None = None) -> FastAPI:
    """http lets tests inject a client with a fake (or in-process) IdP and ERP behind it."""
    settings.validate()
    server = settings.server
    tools = tools_for_server(load_tools(settings.config_dir / "tools.yaml"), server)
    matrix = Matrix(load_yaml(settings.config_dir / "tool-access-matrix.yaml"))

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    setup_service(app, f"mcp-{server}")
    client = http or new_http_client()

    if server == "catalog":
        products = json.loads((settings.catalog_path or default_catalog_path()).read_text())["products"]
        tool_handlers = handlers.catalog_handlers(products)
    else:
        erp = ErpClient(client, erp_base_url=settings.erp_base_url, idp_base_url=settings.idp_base_url,
                        client_id=settings.svc_client_id, client_secret=settings.svc_client_secret)
        if server == "orders":
            personas = Personas.load(settings.config_dir / "personas.yaml")
            tool_handlers = handlers.orders_handlers(erp, personas)
        elif server == "inventory":
            tool_handlers = handlers.inventory_handlers(erp)
        else:
            tool_handlers = handlers.refunds_handlers(erp)

    app.include_router(mcp_router(server_name=f"mcp-{server}", path=f"/mcp/{server}", tools=tools,
                                  handlers=tool_handlers, authorize=make_authorize(server, matrix)))

    if http is None:  # we created the client, so we close it
        app.router.on_shutdown.append(client.aclose)
    return app
