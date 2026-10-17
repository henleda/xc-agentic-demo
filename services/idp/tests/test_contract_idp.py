"""Black-box contract tests for the demo IdP, written from services/idp/README.md and docs/02-agents.md only.

Every test talks to the app through a recording TestClient. After each test the recorder checks, suite-wide:
- every response carries `x-copperline-origin: idp`;
- every 4xx from a token endpoint is JSON with non-empty `error` and `error_description`;
- no client secret, signing-key line or token (sent or issued) appears in captured stdout/stderr (the JSON logs).
"""

import importlib
import json
import secrets
import shutil
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs

import jwt as pyjwt
import pytest
import yaml
from copperline_common.jwt import JwksVerifier, key_id, public_jwks, sign, unverified_claims
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_CONFIG = REPO_ROOT / "config"

ISSUER = "https://idp.contract.test"
EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
TOKEN_PATHS = ("/auth/demo-login", "/auth/token", "/token/exchange")
PRIVATE_JWK_FIELDS = ("d", "p", "q", "dp", "dq", "qi", "oth", "k")
MIN_SENSITIVE_LEN = 16

# Test-only client secrets. bench gets one so its request gets past invalid_client to unauthorized_client.
CLIENT_SECRETS = {
    client_id: f"{client_id}-{secrets.token_urlsafe(24)}"
    for client_id in ("agent-shopper", "agent-storeops", "agent-refunds", "svc-mcp", "granite-supply", "bench")
}
WRONG_SECRET = "WRONG-" + secrets.token_urlsafe(24)

# sub -> (role, name, aud, customer_id), copied by hand from config/personas.yaml
PERSONAS = {
    "alex.rivera": ("customer", "Alex Rivera", "copperline-shop", "C-1001"),
    "mallory.x": ("customer", "Mallory", "copperline-shop", "C-1066"),
    "jordan.lee": ("store_associate", "Jordan Lee", "copperline-ops", None),
    "sam.patel": ("care_agent", "Sam Patel", "copperline-ops", None),
    "morgan.chen": ("care_supervisor", "Morgan Chen", "copperline-ops", None),
}

# The README's claim table is read as exhaustive: these claims, plus iss/iat/exp/jti, and nothing else.
STANDARD_CLAIMS = {"iss", "iat", "exp", "jti", "aud"}
USER_CLAIMS = STANDARD_CLAIMS | {"sub", "role", "name"}
AGENT_CLAIMS = STANDARD_CLAIMS | {"sub", "agent_id", "agent_role", "obo_sub", "obo_role"}
SERVICE_CLAIMS = STANDARD_CLAIMS | {"sub", "svc_role"}
PARTNER_CLAIMS = STANDARD_CLAIMS | {"sub", "role"}


# ---------------------------------------------------------------------------------------------------------------------
# Recorder: watches every request/response and the captured log output
# ---------------------------------------------------------------------------------------------------------------------


def json_lines(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def json_body(response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def is_oauth_error(body: object) -> bool:
    if not isinstance(body, dict):
        return False
    error, description = body.get("error"), body.get("error_description")
    return isinstance(error, str) and bool(error) and isinstance(description, str) and bool(description)


class Recorder:
    def __init__(self, capsys) -> None:
        self.capsys = capsys
        self.sensitive: set[str] = set()
        self.problems: list[str] = []
        self.output = ""

    def add_sensitive(self, *values: str) -> None:
        for value in values:
            if isinstance(value, str) and len(value) >= MIN_SENSITIVE_LEN:
                self.sensitive.add(value)

    def add_token(self, token: str) -> None:
        self.add_sensitive(token)
        parts = token.split(".")
        if len(parts) == 3:
            self.add_sensitive(parts[2])  # the signature alone is enough to count as a leak

    def on_request(self, request) -> None:
        if request.method != "POST" or request.url.path == "/auth/demo-login":
            return
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        self.add_sensitive(*form.get("client_secret", []))
        for token in form.get("subject_token", []):
            self.add_token(token)

    def on_response(self, response) -> None:
        response.read()
        where = f"{response.request.method} {response.request.url.path} -> {response.status_code}"
        if response.headers.get("x-copperline-origin") != "idp":
            self.problems.append(f"{where}: missing x-copperline-origin: idp")
        body = json_body(response)
        if response.request.url.path in TOKEN_PATHS and response.status_code >= 400 and not is_oauth_error(body):
            self.problems.append(f"{where}: error body lacks error/error_description: {response.text[:200]}")
        if isinstance(body, dict) and isinstance(body.get("access_token"), str):
            self.add_token(body["access_token"])

    def read_logs(self) -> list[dict]:
        captured = self.capsys.readouterr()
        text = captured.out + captured.err
        self.output += text
        return json_lines(text)

    def assert_clean(self) -> None:
        self.read_logs()
        leaked = sorted(value[:10] + "..." for value in self.sensitive if value in self.output)
        assert not leaked, f"secret or token values found in log output (prefixes): {leaked}"
        assert not self.problems, "\n".join(self.problems)


# ---------------------------------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def signing_pem(signing_key) -> str:
    pem = signing_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return pem.decode()


def flat_config_dir(target: Path, personas: dict | None = None) -> Path:
    """The REAL personas.yaml and matrix laid out flat, like the vK8s ConfigMap mount.

    The README reads `${CONFIG_DIR}/personas.yaml` and `${CONFIG_DIR}/tool-access-matrix.yaml`, while the repo keeps the
    matrix under config/policy/. The repo layout is covered on its own by test_repo_config_layout_is_accepted.
    """
    if personas is None:
        shutil.copy(REAL_CONFIG / "personas.yaml", target / "personas.yaml")
    else:
        (target / "personas.yaml").write_text(yaml.safe_dump(personas))
    shutil.copy(REAL_CONFIG / "policy" / "tool-access-matrix.yaml", target / "tool-access-matrix.yaml")
    return target


@pytest.fixture(scope="module")
def config_dir(tmp_path_factory) -> Path:
    return flat_config_dir(tmp_path_factory.mktemp("idp-config"))


@pytest.fixture(scope="module")
def idp_module(config_dir, signing_pem):
    """Import idp.app with a valid env, in case the module builds its default `app` from env at import time."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("CONFIG_DIR", str(config_dir))
        mp.setenv("SIGNING_KEY_PEM", signing_pem)
        mp.setenv("ISSUER", ISSUER)
        mp.setenv("CLIENT_SECRETS_JSON", json.dumps(CLIENT_SECRETS))
        return importlib.import_module("idp.app")


@pytest.fixture
def recorder(capsys, signing_pem):
    rec = Recorder(capsys)
    rec.add_sensitive(*CLIENT_SECRETS.values(), WRONG_SECRET)
    rec.add_sensitive(*[line for line in signing_pem.splitlines() if not line.startswith("-----")])
    yield rec
    rec.assert_clean()


@pytest.fixture
def make_client(idp_module, config_dir, signing_pem, recorder):
    """make_client(config=..., client_secrets=...) -> a started TestClient for a fresh app."""
    with ExitStack() as stack:

        def _make(*, config: Path | None = None, client_secrets: dict | None = None) -> TestClient:
            settings = idp_module.Settings(
                config_dir=config or config_dir,
                issuer=ISSUER,
                signing_key_pem=signing_pem,
                client_secrets=dict(CLIENT_SECRETS if client_secrets is None else client_secrets),
            )
            client = stack.enter_context(TestClient(idp_module.create_app(settings)))
            client.event_hooks = {"request": [recorder.on_request], "response": [recorder.on_response]}
            return client

        yield _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


# ---------------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------------


def demo_login(client, sub: str, run_salt: str | None = None):
    body = {"sub": sub} if run_salt is None else {"sub": sub, "run_salt": run_salt}
    return client.post("/auth/demo-login", json=body)


def login(client, sub: str, run_salt: str | None = None) -> str:
    response = demo_login(client, sub, run_salt)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def client_credentials(client, client_id, audience, *, secret=None, grant_type="client_credentials", omit=()):
    form = {
        "grant_type": grant_type,
        "client_id": client_id,
        "client_secret": CLIENT_SECRETS[client_id] if secret is None else secret,
        "audience": audience,
    }
    for name in omit:
        form.pop(name)
    return client.post("/auth/token", data=form)


def exchange(
    client,
    client_id,
    subject_token,
    requested_audience="copperline-tools",
    *,
    secret=None,
    grant_type=EXCHANGE_GRANT,
    subject_token_type=JWT_TOKEN_TYPE,
    omit=(),
):
    form = {
        "grant_type": grant_type,
        "subject_token": subject_token,
        "subject_token_type": subject_token_type,
        "client_id": client_id,
        "client_secret": CLIENT_SECRETS[client_id] if secret is None else secret,
        "requested_audience": requested_audience,
    }
    if subject_token_type is None:
        form.pop("subject_token_type")
    for name in omit:
        form.pop(name)
    return client.post("/token/exchange", data=form)


def agent_token(client, sub="alex.rivera", client_id="agent-shopper", audience="copperline-tools") -> str:
    response = exchange(client, client_id, login(client, sub), audience)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def assert_error(response, status: int, error: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"] == error, response.text


def verify(client, token: str, audience: str) -> dict:
    """Verify against the IdP's own published JWKS (signature, iss, exp, aud)."""
    jwks = client.get("/.well-known/jwks.json").json()
    return JwksVerifier(jwks, issuer=ISSUER).verify(token, audience=audience)


def assert_lifetime(claims: dict, ttl_s: int) -> None:
    assert claims["exp"] - claims["iat"] == ttl_s
    assert abs(claims["iat"] - time.time()) < 60


def assert_flat_strings(claims: dict) -> None:
    for name, value in claims.items():
        if name not in ("iat", "exp"):
            assert isinstance(value, str), f"claim {name} is {type(value).__name__}, expected a flat string"


def forge_user_token(key, *, issuer=ISSUER, now=None, ttl_s=1800, **overrides) -> str:
    claims = {"sub": "alex.rivera", "role": "customer", "name": "Alex Rivera", "aud": "copperline-shop"}
    claims.update(overrides)
    return sign(claims, key, ttl_s=ttl_s, issuer=issuer, now=now)


def bad_subject_token(case: str, key, other_key) -> str:
    if case == "expired":
        return forge_user_token(key, now=int(time.time()) - 3600)  # 30 min lifetime, issued an hour ago
    if case == "signed-by-another-key":
        return forge_user_token(other_key)
    if case == "another-key-claiming-our-kid":
        claims = unverified_claims(forge_user_token(key))
        return pyjwt.encode(claims, other_key, algorithm="RS256", headers={"kid": key_id(key)})
    if case == "wrong-issuer":
        return forge_user_token(key, issuer="https://idp.evil.example")
    if case == "unsigned-alg-none":
        return pyjwt.encode(unverified_claims(forge_user_token(key)), None, algorithm="none")
    if case == "not-a-jwt":
        return "not-a-jwt"
    raise AssertionError(f"unknown case {case}")


# ---------------------------------------------------------------------------------------------------------------------
# Plain endpoints
# ---------------------------------------------------------------------------------------------------------------------


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "idp"}


def test_jwks_publishes_one_public_rs256_key_with_kid(client, signing_key):
    response = client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    keys = response.json()["keys"]
    assert len(keys) == 1
    jwk = keys[0]
    expected = public_jwks(signing_key)["keys"][0]
    assert jwk["kty"] == "RSA"
    assert jwk["alg"] == "RS256"
    assert jwk["kid"] == key_id(signing_key)
    assert (jwk["n"], jwk["e"]) == (expected["n"], expected["e"])
    assert not [field for field in PRIVATE_JWK_FIELDS if field in jwk]


def issue(client, kind: str) -> str:
    if kind == "user-with-customer-id":
        return login(client, "alex.rivera")
    if kind == "user-without-customer-id":
        return login(client, "jordan.lee")  # the persona has store_id; the README lists no such claim
    if kind == "agent":
        return agent_token(client)
    if kind == "machine-mode-agent":
        return client_credentials(client, "agent-storeops", "copperline-tools").json()["access_token"]
    if kind == "service":
        return client_credentials(client, "svc-mcp", "copperline-erp").json()["access_token"]
    if kind == "partner":
        return client_credentials(client, "granite-supply", "copperline-partner").json()["access_token"]
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("user-with-customer-id", USER_CLAIMS | {"customer_id"}),
        ("user-without-customer-id", USER_CLAIMS),
        ("agent", AGENT_CLAIMS),
        ("machine-mode-agent", AGENT_CLAIMS),
        ("service", SERVICE_CLAIMS),
        ("partner", PARTNER_CLAIMS),
    ],
)
def test_tokens_carry_exactly_the_documented_claims(client, kind, expected):
    assert set(unverified_claims(issue(client, kind))) == expected


def test_tokens_are_rs256_with_the_jwks_kid_and_a_fresh_jti(client, signing_key):
    first, second = login(client, "alex.rivera"), login(client, "alex.rivera")
    assert pyjwt.get_unverified_header(first)["alg"] == "RS256"
    assert pyjwt.get_unverified_header(first)["kid"] == key_id(signing_key)
    assert unverified_claims(first)["jti"] != unverified_claims(second)["jti"]


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/admin"),
        ("POST", "/admin"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
        ("GET", "/.well-known/openid-configuration"),
        ("POST", "/auth/login"),
        ("POST", "/token"),
    ],
)
def test_no_undocumented_routes(client, method, path):
    assert client.request(method, path).status_code == 404


def test_every_response_carries_origin_header(client):
    responses = [
        client.get("/healthz"),
        client.get("/.well-known/jwks.json"),
        client.get("/admin"),
        demo_login(client, "alex.rivera"),
        demo_login(client, "nobody"),
        client_credentials(client, "agent-shopper", "copperline-tools", secret=WRONG_SECRET),
        exchange(client, "agent-shopper", "not-a-jwt"),
    ]
    for response in responses:
        assert response.headers.get("x-copperline-origin") == "idp", (response.request.url.path, response.status_code)


# ---------------------------------------------------------------------------------------------------------------------
# /auth/demo-login
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sub", sorted(PERSONAS))
def test_demo_login_issues_user_token_for_every_persona(client, sub):
    role, name, aud, customer_id = PERSONAS[sub]
    response = demo_login(client, sub)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 1800
    assert (body["sub"], body["role"], body["name"]) == (sub, role, name)

    claims = verify(client, body["access_token"], audience=aud)
    assert claims["aud"] == aud
    assert (claims["sub"], claims["role"], claims["name"], claims["iss"]) == (sub, role, name, ISSUER)
    assert_lifetime(claims, 1800)
    assert_flat_strings(claims)
    if customer_id is None:
        assert "customer_id" not in claims
    else:
        assert claims["customer_id"] == customer_id


def test_demo_login_salts_mallory_with_run_salt(client):
    response = demo_login(client, "mallory.x", run_salt="r7x2")
    assert response.status_code == 200, response.text
    assert response.json()["sub"] == "mallory.x-r7x2"
    claims = verify(client, response.json()["access_token"], audience="copperline-shop")
    assert claims["sub"] == "mallory.x-r7x2"
    assert claims["customer_id"] == "C-1066"
    assert claims["role"] == "customer"


@pytest.mark.parametrize("run_salt", [None, ""])
def test_demo_login_without_salt_keeps_mallory_plain(client, run_salt):
    claims = unverified_claims(login(client, "mallory.x", run_salt))
    assert claims["sub"] == "mallory.x"


@pytest.mark.parametrize("sub", ["alex.rivera", "jordan.lee"])
def test_demo_login_ignores_run_salt_for_unsalted_personas(client, sub):
    response = demo_login(client, sub, run_salt="r7x2")
    assert response.status_code == 200, response.text
    assert response.json()["sub"] == sub
    assert unverified_claims(response.json()["access_token"])["sub"] == sub


@pytest.mark.parametrize("sub", ["nobody", "mallory.x-r7x2", "alex.rivera-r7x2", "bench-1", "granite-supply"])
def test_demo_login_unknown_or_already_salted_sub_is_404(client, sub):
    assert_error(demo_login(client, sub), 404, "unknown_user")


# ---------------------------------------------------------------------------------------------------------------------
# /auth/token (client_credentials)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("audience", ["copperline-tools", "copperline-llm"])
@pytest.mark.parametrize(
    "client_id,agent_id", [("agent-storeops", "storeops"), ("agent-shopper", "shopper"), ("agent-refunds", "refunds")]
)
def test_agent_client_credentials_gives_machine_mode_token(client, client_id, agent_id, audience):
    response = client_credentials(client, client_id, audience)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 300

    claims = verify(client, body["access_token"], audience=audience)
    assert claims["sub"] == claims["agent_id"] == agent_id
    assert claims["agent_role"] == agent_id
    assert claims["obo_sub"] == "none"
    assert claims["obo_role"] == "none"
    assert claims["aud"] == audience
    assert_lifetime(claims, 300)
    assert_flat_strings(claims)


def test_service_client_credentials_gives_service_token(client):
    response = client_credentials(client, "svc-mcp", "copperline-erp")
    assert response.status_code == 200, response.text
    assert response.json()["expires_in"] == 300
    claims = verify(client, response.json()["access_token"], audience="copperline-erp")
    assert (claims["sub"], claims["svc_role"], claims["aud"]) == ("mcp-servers", "tool-server", "copperline-erp")
    assert_lifetime(claims, 300)
    assert_flat_strings(claims)


def test_partner_client_credentials_gives_partner_token(client):
    response = client_credentials(client, "granite-supply", "copperline-partner")
    assert response.status_code == 200, response.text
    assert response.json()["expires_in"] == 300
    claims = verify(client, response.json()["access_token"], audience="copperline-partner")
    assert (claims["sub"], claims["role"], claims["aud"]) == ("granite-supply", "partner", "copperline-partner")
    assert_lifetime(claims, 300)
    assert_flat_strings(claims)


@pytest.mark.parametrize(
    "client_id,audience",
    [
        ("agent-storeops", "copperline-erp"),
        ("agent-shopper", "copperline-shop"),
        ("svc-mcp", "copperline-tools"),
        ("svc-mcp", "copperline-llm"),
        ("granite-supply", "copperline-erp"),
        ("granite-supply", "copperline-tools"),
    ],
)
def test_client_credentials_audience_not_allowed_is_invalid_target(client, client_id, audience):
    assert_error(client_credentials(client, client_id, audience), 400, "invalid_target")


def test_bench_client_is_unauthorized_client_in_phase_1(client):
    assert_error(client_credentials(client, "bench", "copperline-shop"), 400, "unauthorized_client")


# ---------------------------------------------------------------------------------------------------------------------
# Client authentication (both grant endpoints)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["/auth/token", "/token/exchange"])
@pytest.mark.parametrize("case", ["wrong-secret", "other-clients-secret", "empty-secret", "missing-secret", "unknown"])
def test_bad_client_authentication_is_401_invalid_client(client, endpoint, case):
    client_id, secret, omit = "agent-shopper", None, ()
    if case == "wrong-secret":
        secret = WRONG_SECRET
    elif case == "other-clients-secret":
        secret = CLIENT_SECRETS["agent-storeops"]
    elif case == "empty-secret":
        secret = ""
    elif case == "missing-secret":
        omit = ("client_secret",)
    elif case == "unknown":
        client_id, secret = "agent-ghost", WRONG_SECRET

    if endpoint == "/auth/token":
        response = client_credentials(client, client_id, "copperline-tools", secret=secret, omit=omit)
    else:
        response = exchange(client, client_id, login(client, "alex.rivera"), secret=secret, omit=omit)
    assert_error(response, 401, "invalid_client")


def test_client_without_configured_secret_always_fails(make_client, monkeypatch):
    monkeypatch.delenv("AGENT_REFUNDS_SECRET", raising=False)
    monkeypatch.delenv("CLIENT_SECRETS_JSON", raising=False)
    client = make_client(client_secrets={k: v for k, v in CLIENT_SECRETS.items() if k != "agent-refunds"})
    for secret in ("", "None", "null", CLIENT_SECRETS["agent-refunds"]):
        response = client_credentials(client, "agent-refunds", "copperline-tools", secret=secret)
        assert_error(response, 401, "invalid_client")
    response = client_credentials(client, "agent-refunds", "copperline-tools", omit=("client_secret",))
    assert_error(response, 401, "invalid_client")


# ---------------------------------------------------------------------------------------------------------------------
# /token/exchange
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sub,run_salt,client_id,audience,agent_id,obo_role",
    [
        ("alex.rivera", None, "agent-shopper", "copperline-tools", "shopper", "customer"),
        ("sam.patel", None, "agent-refunds", "copperline-llm", "refunds", "care_agent"),
        ("morgan.chen", None, "agent-refunds", "copperline-tools", "refunds", "care_supervisor"),
        ("jordan.lee", None, "agent-storeops", "copperline-tools", "storeops", "store_associate"),
        ("mallory.x", "r7x2", "agent-shopper", "copperline-llm", "shopper", "customer"),
    ],
)
def test_exchange_issues_agent_token(client, sub, run_salt, client_id, audience, agent_id, obo_role):
    user_token = login(client, sub, run_salt)
    response = exchange(client, client_id, user_token, audience)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["issued_token_type"] == JWT_TOKEN_TYPE
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 300

    claims = verify(client, body["access_token"], audience=audience)
    assert claims["sub"] == claims["agent_id"] == agent_id
    assert claims["agent_role"] == agent_id
    assert claims["obo_sub"] == (f"{sub}-{run_salt}" if run_salt else sub)
    assert claims["obo_role"] == obo_role
    assert claims["aud"] == audience
    assert_lifetime(claims, 300)
    assert_flat_strings(claims)


def test_exchange_subject_token_type_is_optional(client):
    response = exchange(client, "agent-shopper", login(client, "alex.rivera"), subject_token_type=None)
    assert response.status_code == 200, response.text


def test_exchange_accepts_subject_token_with_any_audience(client, signing_key):
    subject = forge_user_token(signing_key, aud="copperline-ops")
    response = exchange(client, "agent-shopper", subject)
    assert response.status_code == 200, response.text
    assert unverified_claims(response.json()["access_token"])["obo_sub"] == "alex.rivera"


@pytest.mark.parametrize(
    "sub,client_id",
    [
        ("alex.rivera", "agent-refunds"),
        ("jordan.lee", "agent-shopper"),
        ("sam.patel", "agent-storeops"),
        ("morgan.chen", "agent-shopper"),
        ("mallory.x", "agent-storeops"),
    ],
)
@pytest.mark.parametrize("audience", ["copperline-tools", "copperline-llm"])
def test_exchange_role_not_allowed_for_agent_is_unauthorized_user_role(client, sub, client_id, audience):
    response = exchange(client, client_id, login(client, sub), audience)
    assert_error(response, 400, "unauthorized_user_role")


def test_repo_config_layout_is_accepted(make_client):
    # Settings(config_dir=<repo>/config): personas.yaml at the top, the matrix under policy/ (the task brief says to use
    # the real config dir; the README only names the flat ConfigMap layout).
    client = make_client(config=REAL_CONFIG)
    assert exchange(client, "agent-refunds", login(client, "sam.patel")).status_code == 200
    assert_error(exchange(client, "agent-refunds", login(client, "alex.rivera")), 400, "unauthorized_user_role")


def test_agent_client_missing_from_matrix_has_no_allowed_roles(make_client, tmp_path):
    personas = yaml.safe_load((REAL_CONFIG / "personas.yaml").read_text())
    ghost = {"client_id": "agent-ghost", "kind": "agent", "agent_id": "ghost", "agent_role": "ghost"}
    personas["clients"].append(ghost)
    ghost_secret = "agent-ghost-" + secrets.token_urlsafe(24)
    config = flat_config_dir(tmp_path, personas)
    client = make_client(config=config, client_secrets={**CLIENT_SECRETS, "agent-ghost": ghost_secret})

    for sub in PERSONAS:
        response = exchange(client, "agent-ghost", login(client, sub), secret=ghost_secret)
        assert_error(response, 400, "unauthorized_user_role")


@pytest.mark.parametrize("client_id", ["svc-mcp", "granite-supply", "bench"])
def test_exchange_by_non_agent_client_is_unauthorized_client(client, client_id):
    response = exchange(client, client_id, login(client, "alex.rivera"))
    assert_error(response, 400, "unauthorized_client")


@pytest.mark.parametrize(
    "case",
    [
        "expired",
        "signed-by-another-key",
        "another-key-claiming-our-kid",
        "wrong-issuer",
        "unsigned-alg-none",
        "not-a-jwt",
    ],
)
def test_exchange_subject_token_failing_verification_is_invalid_grant(client, signing_key, other_key, case):
    response = exchange(client, "agent-shopper", bad_subject_token(case, signing_key, other_key))
    assert_error(response, 400, "invalid_grant")


def test_exchange_subject_token_that_is_an_agent_token_is_invalid_grant(client):
    response = exchange(client, "agent-shopper", agent_token(client))
    assert_error(response, 400, "invalid_grant")


def test_exchange_subject_token_that_is_a_machine_mode_token_is_invalid_grant(client):
    machine = client_credentials(client, "agent-storeops", "copperline-tools").json()["access_token"]
    assert_error(exchange(client, "agent-storeops", machine), 400, "invalid_grant")


def test_exchange_subject_token_that_is_a_service_token_is_invalid_grant(client):
    service = client_credentials(client, "svc-mcp", "copperline-erp").json()["access_token"]
    assert_error(exchange(client, "agent-shopper", service), 400, "invalid_grant")


def test_exchange_subject_token_that_is_a_partner_token_is_refused(client):
    # The README defines "not a user token" as having agent_id or svc_role; a partner token has neither, but its
    # role (partner) is in no agent's allowed_user_roles. Either refusal is within the contract.
    partner = client_credentials(client, "granite-supply", "copperline-partner").json()["access_token"]
    response = exchange(client, "agent-shopper", partner)
    assert response.status_code == 400, response.text
    assert response.json()["error"] in ("invalid_grant", "unauthorized_user_role")


@pytest.mark.parametrize("extra_claim", [{"agent_id": "shopper"}, {"svc_role": "tool-server"}])
def test_exchange_subject_token_with_agent_or_service_claim_is_invalid_grant(client, signing_key, extra_claim):
    # Otherwise a perfectly good customer token: only the agent_id / svc_role claim makes it "not a user token".
    subject = forge_user_token(signing_key, **extra_claim)
    assert_error(exchange(client, "agent-shopper", subject), 400, "invalid_grant")


@pytest.mark.parametrize("audience", ["copperline-erp", "copperline-shop", "copperline-partner"])
def test_exchange_requested_audience_not_tools_or_llm_is_invalid_target(client, audience):
    response = exchange(client, "agent-shopper", login(client, "alex.rivera"), audience)
    assert_error(response, 400, "invalid_target")


# ---------------------------------------------------------------------------------------------------------------------
# Form errors and check order
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint,grant_type",
    [
        ("/auth/token", "password"),
        ("/auth/token", EXCHANGE_GRANT),
        ("/token/exchange", "client_credentials"),
        ("/token/exchange", "authorization_code"),
    ],
)
def test_wrong_grant_type_is_unsupported_grant_type(client, endpoint, grant_type):
    if endpoint == "/auth/token":
        response = client_credentials(client, "agent-shopper", "copperline-tools", grant_type=grant_type)
    else:
        response = exchange(client, "agent-shopper", login(client, "alex.rivera"), grant_type=grant_type)
    assert_error(response, 400, "unsupported_grant_type")


@pytest.mark.parametrize(
    "endpoint,missing",
    [
        ("/auth/token", "audience"),
        ("/token/exchange", "subject_token"),
        ("/token/exchange", "requested_audience"),
    ],
)
def test_missing_required_field_is_invalid_request(client, endpoint, missing):
    if endpoint == "/auth/token":
        response = client_credentials(client, "agent-shopper", "copperline-tools", omit=(missing,))
    else:
        response = exchange(client, "agent-shopper", login(client, "alex.rivera"), omit=(missing,))
    assert_error(response, 400, "invalid_request")


@pytest.mark.parametrize("endpoint", ["/auth/token", "/token/exchange"])
def test_missing_grant_type_is_a_400_form_error(client, endpoint):
    if endpoint == "/auth/token":
        response = client_credentials(client, "agent-shopper", "copperline-tools", omit=("grant_type",))
    else:
        response = exchange(client, "agent-shopper", login(client, "alex.rivera"), omit=("grant_type",))
    assert response.status_code == 400, response.text
    assert response.json()["error"] in ("invalid_request", "unsupported_grant_type")


def test_order_bad_secret_beats_disallowed_role(client):
    response = exchange(client, "agent-refunds", login(client, "alex.rivera"), secret=WRONG_SECRET)
    assert_error(response, 401, "invalid_client")


def test_order_bad_secret_beats_audience_not_allowed(client):
    response = client_credentials(client, "agent-shopper", "copperline-erp", secret=WRONG_SECRET)
    assert_error(response, 401, "invalid_client")


def test_order_form_errors_beat_bad_secret(client):
    user_token = login(client, "alex.rivera")
    response = exchange(client, "agent-shopper", user_token, secret=WRONG_SECRET, grant_type="client_credentials")
    assert_error(response, 400, "unsupported_grant_type")
    response = exchange(client, "agent-shopper", user_token, secret=WRONG_SECRET, omit=("requested_audience",))
    assert_error(response, 400, "invalid_request")
    response = client_credentials(client, "agent-shopper", "copperline-tools", secret=WRONG_SECRET, omit=("audience",))
    assert_error(response, 400, "invalid_request")


def test_order_non_agent_client_beats_bad_subject_token(client):
    assert_error(exchange(client, "svc-mcp", "not-a-jwt", "copperline-erp"), 400, "unauthorized_client")


def test_order_bad_subject_token_beats_bad_audience(client, signing_key, other_key):
    expired = bad_subject_token("expired", signing_key, other_key)
    assert_error(exchange(client, "agent-shopper", expired, "copperline-erp"), 400, "invalid_grant")
    assert_error(exchange(client, "agent-shopper", agent_token(client), "copperline-erp"), 400, "invalid_grant")


def test_order_bad_audience_beats_disallowed_role(client):
    response = exchange(client, "agent-refunds", login(client, "alex.rivera"), "copperline-erp")
    assert_error(response, 400, "invalid_target")


def test_every_error_body_has_error_and_description(client, signing_key, other_key):
    user_token = login(client, "alex.rivera")
    responses = [
        client_credentials(client, "agent-shopper", "copperline-tools", secret=WRONG_SECRET),
        client_credentials(client, "agent-shopper", "copperline-erp"),
        client_credentials(client, "agent-shopper", "copperline-tools", grant_type="password"),
        client_credentials(client, "agent-shopper", "copperline-tools", omit=("audience",)),
        client_credentials(client, "bench", "copperline-shop"),
        exchange(client, "svc-mcp", user_token),
        exchange(client, "agent-shopper", bad_subject_token("expired", signing_key, other_key)),
        exchange(client, "agent-shopper", user_token, "copperline-erp"),
        exchange(client, "agent-refunds", user_token),
        demo_login(client, "nobody"),
    ]
    for response in responses:
        assert 400 <= response.status_code < 500, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert is_oauth_error(response.json()), response.text


# ---------------------------------------------------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------------------------------------------------


def test_issued_token_is_logged_with_documented_fields(client, recorder):
    user_token = login(client, "alex.rivera")
    recorder.read_logs()
    response = exchange(client, "agent-shopper", user_token)
    jti = unverified_claims(response.json()["access_token"])["jti"]

    issued = [record for record in recorder.read_logs() if record.get("event") == "token_issued"]
    assert len(issued) == 1, issued
    record = issued[0]
    for name in ("grant", "client_id", "aud", "sub", "agent_role", "obo_sub", "obo_role", "jti"):
        assert name in record, f"token_issued lacks {name}: {record}"
    assert record["client_id"] == "agent-shopper"
    assert record["aud"] == "copperline-tools"
    assert record["sub"] == "shopper"
    assert record["agent_role"] == "shopper"
    assert record["obo_sub"] == "alex.rivera"
    assert record["obo_role"] == "customer"
    assert record["jti"] == jti
    assert record["grant"]


def test_refusals_are_logged_with_documented_fields(client, recorder):
    recorder.read_logs()
    client_credentials(client, "agent-shopper", "copperline-tools", secret=WRONG_SECRET)
    demo_login(client, "nobody")

    refused = [record for record in recorder.read_logs() if record.get("event") == "token_refused"]
    for record in refused:
        for name in ("grant", "error", "status"):
            assert name in record, f"token_refused lacks {name}: {record}"
    client_refusals = [record for record in refused if record["error"] == "invalid_client"]
    assert len(client_refusals) == 1, refused
    assert client_refusals[0].get("client_id") == "agent-shopper"
    assert client_refusals[0]["status"] == 401
    assert [record["status"] for record in refused if record["error"] == "unknown_user"] == [404]


def test_no_token_or_secret_in_logs_across_a_full_session(client, recorder, signing_key):
    user_token = login(client, "alex.rivera")
    exchange(client, "agent-shopper", user_token)
    exchange(client, "agent-shopper", user_token, "copperline-llm")
    exchange(client, "agent-refunds", user_token)
    exchange(client, "agent-shopper", user_token, secret=WRONG_SECRET)
    exchange(client, "agent-shopper", forge_user_token(signing_key, issuer="https://idp.evil.example"))
    client_credentials(client, "agent-storeops", "copperline-tools")
    client_credentials(client, "svc-mcp", "copperline-erp")
    client_credentials(client, "granite-supply", "copperline-partner")
    client_credentials(client, "granite-supply", "copperline-partner", secret=WRONG_SECRET)
    login(client, "mallory.x", "r7x2")

    recorder.read_logs()
    assert '"token_issued"' in recorder.output, "no token_issued log lines captured: the leak check would be vacuous"
    assert '"token_refused"' in recorder.output
    recorder.assert_clean()
