"""Calls to the demo IdP and an in-memory token cache.

Token claims are read without verification: the agent trusts the IdP response because that hop goes
through an XC LB.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from copperline_common.jwt import unverified_claims
from copperline_common.outbound import HttpResult, send

GRANT_CLIENT_CREDENTIALS = "client_credentials"
GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"


@dataclass
class TokenResult:
    token: str | None
    claims: dict
    http: HttpResult

    @property
    def expires_at(self) -> int:
        return int(self.claims.get("exp") or 0)


def _token_result(http: HttpResult) -> TokenResult:
    token = None
    if http.status is not None and 200 <= http.status < 300 and isinstance(http.json, dict):
        access_token = http.json.get("access_token")
        token = access_token if isinstance(access_token, str) and access_token else None
    return TokenResult(token=token, claims=unverified_claims(token) if token else {}, http=http)


async def client_credentials(
    http: httpx.AsyncClient, url: str, client_id: str, client_secret: str | None, audience: str
) -> TokenResult:
    form = {"grant_type": GRANT_CLIENT_CREDENTIALS, "client_id": client_id, "audience": audience}
    if client_secret is not None:
        form["client_secret"] = client_secret
    return _token_result(await send(http, "POST", url, data=form))


async def exchange(
    http: httpx.AsyncClient,
    idp_base_url: str,
    *,
    client_id: str,
    client_secret: str | None,
    subject_token: str,
    requested_audience: str,
) -> TokenResult:
    form = {
        "grant_type": GRANT_TOKEN_EXCHANGE,
        "subject_token": subject_token,
        "subject_token_type": TOKEN_TYPE_JWT,
        "client_id": client_id,
        "requested_audience": requested_audience,
    }
    if client_secret is not None:
        form["client_secret"] = client_secret
    url = f"{idp_base_url.rstrip('/')}/token/exchange"
    return _token_result(await send(http, "POST", url, data=form))


class TokenStore:
    """Tokens cached per (obo_sub, aud). A token is served only while exp - skew_s > now."""

    def __init__(self, skew_s: int = 30, clock: Callable[[], float] = time.time) -> None:
        self.skew_s = skew_s
        self.clock = clock
        self._tokens: dict[tuple[str, str], tuple[str, int]] = {}

    def get(self, obo_sub: str, aud: str) -> str | None:
        entry = self._tokens.get((obo_sub, aud))
        if entry is None:
            return None
        token, exp = entry
        if exp - self.skew_s > self.clock():
            return token
        del self._tokens[(obo_sub, aud)]
        return None

    def put(self, obo_sub: str, aud: str, token: str, exp: int) -> None:
        self._tokens[(obo_sub, aud)] = (token, exp)
