"""partner-api settings, read once from env at startup. Tests build a Settings directly."""

import os
from dataclasses import dataclass

from copperline_common.jwt import JwksVerifier

DEFAULT_ISSUER = "https://idp.copperline.demo"


@dataclass
class Settings:
    issuer: str = DEFAULT_ISSUER
    jwks_path: str | None = None  # a JWKS file ...
    idp_base_url: str | None = None  # ... or else ${IDP_BASE_URL}/.well-known/jwks.json (in vK8s, via idp-lb)
    port: int = 8080

    @classmethod
    def from_env(cls) -> "Settings":
        """Raises RuntimeError on a value partner-api cannot start with."""
        settings = cls(
            issuer=os.environ.get("ISSUER") or DEFAULT_ISSUER,
            jwks_path=os.environ.get("JWKS_PATH") or None,
            idp_base_url=os.environ.get("IDP_BASE_URL") or None,
            port=int(os.environ.get("PORT") or 8080),
        )
        settings.jwks_source()  # fail at startup, not on the first request
        return settings

    def jwks_source(self) -> str:
        if self.jwks_path:
            return self.jwks_path
        if self.idp_base_url:
            return f"{self.idp_base_url.rstrip('/')}/.well-known/jwks.json"
        raise RuntimeError("no token keys: set JWKS_PATH (a JWKS file) or IDP_BASE_URL")

    def verifier(self) -> JwksVerifier:
        if self.jwks_path:
            return JwksVerifier(jwks_path=self.jwks_path, issuer=self.issuer)
        return JwksVerifier(jwks_url=self.jwks_source(), issuer=self.issuer)
