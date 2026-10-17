"""The IdP's rules: who may get which token, and with which claims. No HTTP in here (see app.py).

Each grant method runs its checks in the order of the table in services/idp/README.md and raises OAuthError
at the first failure. Every claim is a flat string, because XC service policies match on them.
"""

import hmac
import re
import uuid
from pathlib import Path

from copperline_common.config import load_yaml
from copperline_common.credentials import client_secret
from copperline_common.jwt import JwksVerifier, JwtError, load_private_key, public_jwks, sign
from copperline_common.personas import Personas, salted_sub

from idp.settings import Settings

USER_TTL_S = 1800
AGENT_TTL_S = 300
CLIENT_TTL_S = 300  # machine-mode agent, service and partner tokens

MATRIX_FILE = "tool-access-matrix.yaml"
AGENT_AUDIENCES = ("copperline-tools", "copperline-llm")
ISSUED_CLIENT_KINDS = ("agent", "service", "partner")  # "bench" is not issued until Phase 4
MACHINE_MODE = "none"  # obo_sub / obo_role of a machine-mode token: the literal string, never null (docs/08 R35)

# No "-" allowed: Personas.base_sub() splits "mallory.x-<salt>" on the LAST "-", so a salt with a dash
# would stop the MCP servers from mapping the salted sub back to Mallory.
RUN_SALT_PATTERN = re.compile(r"[A-Za-z0-9_]{1,32}")


class OAuthError(Exception):
    """A refusal. Becomes {"error": error, "error_description": description} with this HTTP status."""

    def __init__(self, status: int, error: str, description: str) -> None:
        super().__init__(description)
        self.status = status
        self.error = error
        self.description = description


class Idp:
    """Loaded once at startup: personas and clients, the access matrix, client secrets and the signing key."""

    def __init__(self, settings: Settings) -> None:
        self.issuer = settings.issuer
        self.personas = Personas.load(settings.config_dir / "personas.yaml")
        self.allowed_user_roles = load_allowed_user_roles(matrix_path(settings.config_dir))
        self.client_secrets = settings.client_secrets
        self.private_key = load_private_key(settings.signing_key_pem)
        self.jwks = public_jwks(self.private_key)
        self.verifier = JwksVerifier(self.jwks, issuer=self.issuer)

    def sign_token(self, claims: dict, ttl_s: int) -> str:
        return sign(claims, self.private_key, ttl_s=ttl_s, issuer=self.issuer)

    # ---- grants -----------------------------------------------------------------------------------------------

    def user_claims(self, sub: str, run_salt: str | None) -> dict:
        """POST /auth/demo-login. `sub` must be a base persona: an already-salted sub is unknown."""
        persona = self.personas.get(sub)
        if persona is None:
            raise OAuthError(404, "unknown_user", f"no demo persona with sub {sub!r}")
        token_sub = sub
        if persona.get("salted") and run_salt:
            if not RUN_SALT_PATTERN.fullmatch(run_salt):
                raise OAuthError(400, "invalid_request", "run_salt must be 1-32 letters, digits or underscores")
            token_sub = salted_sub(sub, run_salt)
        claims = {
            "sub": token_sub,
            "role": persona["role"],
            "name": persona["name"],
            "aud": persona["audiences"][0],
        }
        if persona.get("customer_id"):
            claims["customer_id"] = persona["customer_id"]
        return with_jti(claims)

    def client_credentials_claims(self, client_id: str, secret: str | None, audience: str) -> dict:
        """POST /auth/token: machine-mode agent, service or partner token, depending on the client kind."""
        client = self.authenticate(client_id, secret)
        kind = client.get("kind")
        if kind not in ISSUED_CLIENT_KINDS:
            raise OAuthError(400, "unauthorized_client", f"clients of kind {kind!r} are not issued tokens")
        if audience not in self.allowed_audiences(client):
            raise OAuthError(400, "invalid_target", f"audience {audience!r} is not allowed for {client_id}")
        if kind == "agent":
            return agent_claims(client, obo_sub=MACHINE_MODE, obo_role=MACHINE_MODE, audience=audience)
        if kind == "service":
            return with_jti({"sub": client["sub"], "svc_role": client["svc_role"], "aud": audience})
        return with_jti({"sub": client["sub"], "role": client["role"], "aud": audience})

    def exchange_claims(self, client_id: str, secret: str | None, subject_token: str, requested_audience: str) -> dict:
        """POST /token/exchange: an agent token on behalf of the user in subject_token."""
        client = self.authenticate(client_id, secret)
        if client.get("kind") != "agent":
            raise OAuthError(400, "unauthorized_client", f"{client_id} is not an agent client")
        user = self.verify_user_token(subject_token)
        if requested_audience not in AGENT_AUDIENCES:
            raise OAuthError(400, "invalid_target", f"requested_audience must be one of {', '.join(AGENT_AUDIENCES)}")
        agent_id = client["agent_id"]
        if user["role"] not in self.allowed_user_roles.get(agent_id, []):
            raise OAuthError(
                400, "unauthorized_user_role", f"agent {agent_id} may not act for a user with role {user['role']!r}"
            )
        return agent_claims(client, obo_sub=user["sub"], obo_role=user["role"], audience=requested_audience)

    # ---- checks -----------------------------------------------------------------------------------------------

    def authenticate(self, client_id: str, secret: str | None) -> dict:
        """The client, if its secret matches. Unknown client, no configured secret or a wrong secret: 401."""
        client = self.personas.client(client_id)
        expected = self.secret_for(client_id) if client else None
        # Always run the constant-time compare, so timing reveals neither the secret nor which clients exist.
        matches = hmac.compare_digest((secret or "").encode("utf-8"), (expected or "").encode("utf-8"))
        if client is None or not expected or not matches:
            raise OAuthError(401, "invalid_client", "client authentication failed")
        return client

    def secret_for(self, client_id: str) -> str | None:
        """CLIENT_SECRETS_JSON when it is set; otherwise env <CLIENT_ID>_SECRET."""
        if self.client_secrets is not None:
            return self.client_secrets.get(client_id)
        return client_secret(client_id)

    def allowed_audiences(self, client: dict) -> list[str]:
        if client.get("kind") == "agent":
            return list(AGENT_AUDIENCES)
        return list(client.get("audiences") or [])

    def verify_user_token(self, token: str) -> dict:
        """Signature (our key), iss and exp; any audience. Agent and service tokens are not user tokens."""
        try:
            claims = self.verifier.verify(token, audience=None)
        except JwtError as e:
            raise OAuthError(400, "invalid_grant", f"subject_token rejected: {e}") from None
        if "agent_id" in claims or "svc_role" in claims:
            raise OAuthError(400, "invalid_grant", "subject_token is not a user token")
        if not is_text(claims.get("sub")) or not is_text(claims.get("role")):
            raise OAuthError(400, "invalid_grant", "subject_token has no sub or role")
        return claims


def agent_claims(client: dict, *, obo_sub: str, obo_role: str, audience: str) -> dict:
    agent_id = client["agent_id"]
    return with_jti(
        {
            "sub": agent_id,
            "agent_id": agent_id,
            "agent_role": client["agent_role"],
            "obo_sub": obo_sub,
            "obo_role": obo_role,
            "aud": audience,
        }
    )


def with_jti(claims: dict) -> dict:
    """Set jti here (sign() would add one too) so the token_issued log line can carry it."""
    return {**claims, "jti": uuid.uuid4().hex}


def is_text(value: object) -> bool:
    return isinstance(value, str) and value != ""


def matrix_path(config_dir: Path) -> Path:
    """The flat ConfigMap layout (<dir>/tool-access-matrix.yaml) first, then the repo layout
    (CONFIG_DIR=config, matrix under policy/) for laptop runs. Neither: FileNotFoundError names the first."""
    flat = config_dir / MATRIX_FILE
    repo_layout = config_dir / "policy" / MATRIX_FILE
    return repo_layout if not flat.is_file() and repo_layout.is_file() else flat


def load_allowed_user_roles(path: Path) -> dict[str, list[str]]:
    """agent_id -> allowed_user_roles from the access matrix. An agent missing from it gets no roles."""
    matrix = load_yaml(path) or {}
    return {
        agent_id: list(entry.get("allowed_user_roles") or [])
        for agent_id, entry in (matrix.get("agents") or {}).items()
    }
