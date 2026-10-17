"""Demo IdP HTTP API. Contract: services/idp/README.md. Run with `uvicorn idp.app:app --host 0.0.0.0 --port 8080`.

The rules live in core.py; this module only parses requests, shapes responses and logs outcomes.
"""

from contextlib import asynccontextmanager

from copperline_common.logging import log_event
from copperline_common.service import setup_service
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from idp.core import (
    AGENT_TTL_S,
    CLIENT_TTL_S,
    ISSUED_CLIENT_KINDS,
    USER_TTL_S,
    Idp,
    OAuthError,
)
from idp.settings import Settings

__all__ = ["Settings", "app", "create_app"]

SERVICE = "idp"
GRANT_CLIENT_CREDENTIALS = "client_credentials"
GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
NO_STORE = {"Cache-Control": "no-store"}  # RFC 6749 5.1: token responses are never cached

# Our short names for the `grant` field of token_issued / token_refused log lines.
LOG_GRANT_LOGIN = "demo_login"
LOG_GRANT_CLIENT_CREDENTIALS = "client_credentials"
LOG_GRANT_EXCHANGE = "token_exchange"

HTTP_ERROR_CODES = {400: "invalid_request", 404: "not_found", 405: "method_not_allowed"}


def create_app(settings: Settings | None = None) -> FastAPI:
    """With settings (tests): ready at once. Without: settings come from env at startup, and startup fails
    (so the IdP refuses to start) when there is no usable signing key."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "idp", None) is None:
            app.state.idp = Idp(Settings.from_env())
        log_ready(app.state.idp)
        yield

    # No /docs, /redoc or /openapi.json: the README lists every route there is.
    app = FastAPI(title="copperline-idp", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    if settings is not None:
        app.state.idp = Idp(settings)
    setup_service(app, SERVICE)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_api_route("/auth/demo-login", demo_login, methods=["POST"])
    app.add_api_route("/auth/token", token, methods=["POST"])
    app.add_api_route("/token/exchange", token_exchange, methods=["POST"])
    app.add_api_route("/.well-known/jwks.json", jwks, methods=["GET"])
    return app


# ---- routes -------------------------------------------------------------------------------------------------------


async def demo_login(request: Request) -> JSONResponse:
    idp: Idp = request.app.state.idp
    try:
        sub, run_salt = await read_login_body(request)
        claims = idp.user_claims(sub, run_salt)
    except OAuthError as e:
        return refused(e, grant=LOG_GRANT_LOGIN, client_id=None)
    access_token = issue(idp, claims, USER_TTL_S, grant=LOG_GRANT_LOGIN, client_id=None)
    body = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": USER_TTL_S,
        "sub": claims["sub"],
        "role": claims["role"],
        "name": claims["name"],
    }
    return JSONResponse(body, headers=NO_STORE)


async def token(request: Request) -> JSONResponse:
    idp: Idp = request.app.state.idp
    form = await read_form(request)
    client_id = form.get("client_id")
    try:
        require_grant(form, GRANT_CLIENT_CREDENTIALS)
        require_fields(form, "client_id", "audience")
        claims = idp.client_credentials_claims(client_id, form.get("client_secret"), form["audience"])
    except OAuthError as e:
        return refused(e, grant=LOG_GRANT_CLIENT_CREDENTIALS, client_id=client_id)
    access_token = issue(idp, claims, CLIENT_TTL_S, grant=LOG_GRANT_CLIENT_CREDENTIALS, client_id=client_id)
    body = {"access_token": access_token, "token_type": "Bearer", "expires_in": CLIENT_TTL_S}
    return JSONResponse(body, headers=NO_STORE)


async def token_exchange(request: Request) -> JSONResponse:
    idp: Idp = request.app.state.idp
    form = await read_form(request)
    client_id = form.get("client_id")
    try:
        require_grant(form, GRANT_TOKEN_EXCHANGE)
        require_fields(form, "subject_token", "client_id", "requested_audience")
        claims = idp.exchange_claims(
            client_id, form.get("client_secret"), form["subject_token"], form["requested_audience"]
        )
    except OAuthError as e:
        return refused(e, grant=LOG_GRANT_EXCHANGE, client_id=client_id)
    access_token = issue(idp, claims, AGENT_TTL_S, grant=LOG_GRANT_EXCHANGE, client_id=client_id)
    body = {
        "access_token": access_token,
        "issued_token_type": TOKEN_TYPE_JWT,
        "token_type": "Bearer",
        "expires_in": AGENT_TTL_S,
    }
    return JSONResponse(body, headers=NO_STORE)


async def jwks(request: Request) -> dict:
    return request.app.state.idp.jwks


async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """404, 405 and body-parse errors get the same {"error", "error_description"} shape as refusals."""
    error = HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    body = {"error": error, "error_description": str(exc.detail)}
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


# ---- request parsing ----------------------------------------------------------------------------------------------


async def read_form(request: Request) -> dict[str, str]:
    """The form fields as plain strings. A non-form body reads as an empty form (so: invalid_request)."""
    form = await request.form()
    return {name: value for name, value in form.items() if isinstance(value, str)}


async def read_login_body(request: Request) -> tuple[str, str | None]:
    """(sub, run_salt) from the JSON body {"sub": str, "run_salt": str?}."""
    try:
        body = await request.json()
    except ValueError:
        raise OAuthError(400, "invalid_request", 'body must be JSON: {"sub": "...", "run_salt": "..."}') from None
    if not isinstance(body, dict) or not isinstance(body.get("sub"), str) or not body["sub"]:
        raise OAuthError(400, "invalid_request", "sub is required")
    run_salt = body.get("run_salt")
    if run_salt is not None and not isinstance(run_salt, str):
        raise OAuthError(400, "invalid_request", "run_salt must be a string")
    return body["sub"], run_salt


def require_grant(form: dict[str, str], expected: str) -> None:
    grant_type = form.get("grant_type")
    if not grant_type:
        raise OAuthError(400, "invalid_request", "grant_type is required")
    if grant_type != expected:
        raise OAuthError(400, "unsupported_grant_type", f"this endpoint only accepts grant_type={expected}")


def require_fields(form: dict[str, str], *names: str) -> None:
    missing = [name for name in names if not form.get(name)]
    if missing:
        raise OAuthError(400, "invalid_request", f"missing form field(s): {', '.join(missing)}")


# ---- responses and logs -------------------------------------------------------------------------------------------


def issue(idp: Idp, claims: dict, ttl_s: int, *, grant: str, client_id: str | None) -> str:
    """Sign the token and log token_issued. The token itself is never logged."""
    access_token = idp.sign_token(claims, ttl_s)
    log_event(
        "token_issued",
        status=200,
        grant=grant,
        client_id=client_id,
        agent_id=claims.get("agent_id"),
        aud=claims["aud"],
        sub=claims["sub"],
        agent_role=claims.get("agent_role"),
        obo_sub=claims.get("obo_sub"),
        obo_role=claims.get("obo_role"),
        jti=claims["jti"],
    )
    return access_token


def refused(error: OAuthError, *, grant: str, client_id: str | None) -> JSONResponse:
    """Log token_refused and build the OAuth-style error body. Secrets and tokens never reach the log."""
    log_event(
        "token_refused",
        level="warning",
        status=error.status,
        grant=grant,
        client_id=client_id,
        error=error.error,
        reason=error.description,
    )
    body = {"error": error.error, "error_description": error.description}
    return JSONResponse(body, status_code=error.status, headers=NO_STORE)


def log_ready(idp: Idp) -> None:
    """One startup line. Names the issuable clients that have no secret (they will always get invalid_client)."""
    unconfigured = [
        client["client_id"]
        for client in idp.personas.clients
        if client.get("kind") in ISSUED_CLIENT_KINDS and not idp.secret_for(client["client_id"])
    ]
    log_event(
        "idp_ready",
        level="warning" if unconfigured else "info",
        issuer=idp.issuer,
        kid=idp.jwks["keys"][0]["kid"],
        personas=len(idp.personas.personas),
        clients=len(idp.personas.clients),
        unconfigured_clients=unconfigured,
    )


app = create_app()
