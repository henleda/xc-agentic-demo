"""Client for erp-mock (through erp-lb in the tenant, the local-gateway on a laptop).

Gets a service token from the IdP (client credentials, aud copperline-erp), caches it, refreshes once on a 401,
and turns ERP answers into tool outcomes: 404/409/422 are ToolErrors the model can read, anything else is an
UpstreamError (HTTP 502 to the agent). Response bodies are never logged: they hold PANs and emails.
"""

import asyncio
from urllib.parse import quote

import httpx
from copperline_common.mcp import ToolError, UpstreamError
from copperline_common.outbound import HttpResult, send
from copperline_common.tokens import TokenStore, client_credentials

ERP_AUDIENCE = "copperline-erp"


class ErpClient:
    def __init__(self, http: httpx.AsyncClient, *, erp_base_url: str, idp_base_url: str, client_id: str,
                 client_secret: str) -> None:
        self.http = http
        self.erp_base_url = erp_base_url.rstrip("/")
        self.token_url = f"{idp_base_url.rstrip('/')}/auth/token"
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = TokenStore()
        self._token_lock = asyncio.Lock()  # one IdP call at a time, so concurrent tool calls don't stampede it

    async def _token(self, *, force: bool = False) -> str:
        async with self._token_lock:
            cached = None if force else self.tokens.get(self.client_id, ERP_AUDIENCE)
            if cached:
                return cached
            result = await client_credentials(self.http, self.token_url, self.client_id, self.client_secret,
                                              ERP_AUDIENCE)
            if result.token is None:
                raise UpstreamError("ERP unavailable: could not get a service token", result.http.status)
            self.tokens.put(self.client_id, ERP_AUDIENCE, result.token, result.expires_at)
            return result.token

    async def _call(self, method: str, path: str, json: dict | None = None) -> HttpResult:
        url = f"{self.erp_base_url}{path}"
        token = await self._token()
        result = await send(self.http, method, url, json=json, headers={"Authorization": f"Bearer {token}"})
        if result.status == 401:  # token revoked or keys rotated: refresh exactly once
            token = await self._token(force=True)
            result = await send(self.http, method, url, json=json, headers={"Authorization": f"Bearer {token}"})
        return result

    async def request(self, method: str, path: str, json: dict | None = None, *, what: str = "resource") -> dict:
        """Call the ERP and return its JSON body, or raise the ToolError / UpstreamError the README maps it to."""
        result = await self._call(method, path, json)
        if result.status is not None and 200 <= result.status < 300 and isinstance(result.json, dict):
            return result.json
        if result.status == 404:
            raise ToolError(f"{what} not found")
        if result.status == 409:
            raise ToolError(f"insufficient stock: {_detail(result)}")
        if result.status == 422:
            raise ToolError(f"invalid request: {_first_validation_message(result)}")
        raise UpstreamError("ERP unavailable", result.status)


def segment(value: str) -> str:
    """Quote one URL path segment, so an order_id like '../admin/state' cannot change the ERP path.

    "", "." and ".." are refused outright: quoting leaves them alone and httpx then removes dot segments from the
    URL, which would call a different ERP route."""
    text = str(value)
    if text in ("", ".", ".."):
        raise ToolError("invalid request: identifier must not be empty, '.' or '..'")
    return quote(text, safe="")


def _detail(result: HttpResult) -> str:
    body = result.json if isinstance(result.json, dict) else {}
    return str(body.get("detail") or body.get("error") or "conflict")


def _first_validation_message(result: HttpResult) -> str:
    body = result.json if isinstance(result.json, dict) else {}
    detail = body.get("detail")
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        where = ".".join(str(part) for part in detail[0].get("loc", [])[1:])
        return f"{where}: {detail[0].get('msg', 'invalid')}" if where else str(detail[0].get("msg", "invalid"))
    return str(detail or "invalid")
