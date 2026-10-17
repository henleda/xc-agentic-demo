"""Implementation tests for the demo IdP: the README rules that the demo depends on, plus our own choices.

Test keys are generated here; nothing reads .secrets/. Config is the real personas.yaml and access matrix.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from copperline_common.jwt import JwksVerifier, public_jwks, sign, unverified_claims
from copperline_common.personas import Personas
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from idp import core
from idp.app import create_app
from idp.settings import Settings

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
ISSUER = "https://idp.impl.test"
EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
SECRETS = {
    "agent-shopper": "shopper-secret-0123456789",
    "agent-storeops": "storeops-secret-0123456789",
    "agent-refunds": "refunds-secret-0123456789",
    "svc-mcp": "svc-mcp-secret-0123456789",
    "granite-supply": "granite-secret-0123456789",
    "bench": "bench-secret-0123456789",
}


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def flat_config(tmp_path_factory) -> Path:
    """Like the vK8s ConfigMap mount: personas.yaml and tool-access-matrix.yaml side by side."""
    target = tmp_path_factory.mktemp("config")
    shutil.copy(REPO_CONFIG / "personas.yaml", target / "personas.yaml")
    shutil.copy(REPO_CONFIG / "policy" / "tool-access-matrix.yaml", target / "tool-access-matrix.yaml")
    return target


@pytest.fixture
def settings(flat_config, pem) -> Settings:
    return Settings(config_dir=flat_config, issuer=ISSUER, signing_key_pem=pem, client_secrets=dict(SECRETS))


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client, sub: str, run_salt: str | None = None) -> str:
    body = {"sub": sub} if run_salt is None else {"sub": sub, "run_salt": run_salt}
    response = client.post("/auth/demo-login", json=body)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def exchange(client, client_id: str, subject_token: str, audience: str = "copperline-tools", secret: str | None = None):
    form = {
        "grant_type": EXCHANGE_GRANT,
        "subject_token": subject_token,
        "client_id": client_id,
        "client_secret": SECRETS[client_id] if secret is None else secret,
        "requested_audience": audience,
    }
    return client.post("/token/exchange", data=form)


def client_credentials(client, client_id: str, audience: str, secret: str | None = None):
    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": SECRETS[client_id] if secret is None else secret,
        "audience": audience,
    }
    return client.post("/auth/token", data=form)


def verified(client, token: str, audience: str) -> dict:
    jwks = client.get("/.well-known/jwks.json").json()
    return JwksVerifier(jwks, issuer=ISSUER).verify(token, audience=audience)


def log_records(capsys) -> list[dict]:
    records = []
    for line in capsys.readouterr().out.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


# ---- the rules the demo depends on ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sub,client_id",
    [
        ("alex.rivera", "agent-refunds"),  # a customer may not use the refunds agent
        ("jordan.lee", "agent-shopper"),  # a store associate may not use the shopper agent
    ],
)
def test_disallowed_user_role_is_400_unauthorized_user_role(client, sub, client_id):
    response = exchange(client, client_id, login(client, sub))
    assert response.status_code == 400
    assert response.json()["error"] == "unauthorized_user_role"
    assert "access_token" not in response.json()


def test_allowed_user_role_gets_an_agent_token(client):
    response = exchange(client, "agent-refunds", login(client, "morgan.chen"), "copperline-llm")
    assert response.status_code == 200, response.text
    claims = verified(client, response.json()["access_token"], "copperline-llm")
    assert (claims["sub"], claims["agent_id"], claims["agent_role"]) == ("refunds", "refunds", "refunds")
    assert (claims["obo_sub"], claims["obo_role"]) == ("morgan.chen", "care_supervisor")


@pytest.mark.parametrize("audience", ["copperline-tools", "copperline-llm"])
def test_machine_mode_token_has_literal_none_obo_claims(client, audience):
    response = client_credentials(client, "agent-storeops", audience)
    assert response.status_code == 200, response.text
    claims = verified(client, response.json()["access_token"], audience)
    assert claims["obo_sub"] == "none" and isinstance(claims["obo_sub"], str)
    assert claims["obo_role"] == "none" and isinstance(claims["obo_role"], str)
    assert (claims["sub"], claims["agent_id"], claims["agent_role"]) == ("storeops", "storeops", "storeops")
    assert claims["exp"] - claims["iat"] == 300


def test_exchanged_token_keeps_mallorys_salted_sub(client):
    user_token = login(client, "mallory.x", run_salt="run42")
    assert unverified_claims(user_token)["sub"] == "mallory.x-run42"

    response = exchange(client, "agent-shopper", user_token)
    assert response.status_code == 200, response.text
    claims = verified(client, response.json()["access_token"], "copperline-tools")
    assert claims["obo_sub"] == "mallory.x-run42"
    assert claims["obo_role"] == "customer"
    # The MCP servers map the salted sub back to Mallory's customer record.
    assert Personas.load(REPO_CONFIG / "personas.yaml").customer_id(claims["obo_sub"]) == "C-1066"


def test_user_token_ttl_audience_and_claims(client):
    claims = verified(client, login(client, "alex.rivera"), "copperline-shop")
    assert claims["exp"] - claims["iat"] == 1800
    assert claims["customer_id"] == "C-1001"
    assert claims["iss"] == ISSUER
    ops_claims = verified(client, login(client, "jordan.lee"), "copperline-ops")
    assert "customer_id" not in ops_claims and "store_id" not in ops_claims


def test_service_and_partner_tokens(client):
    service = verified(client, client_credentials(client, "svc-mcp", "copperline-erp").json()["access_token"],
                       "copperline-erp")
    assert (service["sub"], service["svc_role"]) == ("mcp-servers", "tool-server")
    partner = verified(client, client_credentials(client, "granite-supply", "copperline-partner").json()
                       ["access_token"], "copperline-partner")
    assert (partner["sub"], partner["role"]) == ("granite-supply", "partner")


def test_bench_client_is_refused_even_with_a_valid_secret(client):
    response = client_credentials(client, "bench", "copperline-shop")
    assert response.status_code == 400
    assert response.json()["error"] == "unauthorized_client"


# ---- our implementation choices -------------------------------------------------------------------------------------


@pytest.mark.parametrize("run_salt", ["has-dash", "x" * 33, "sp ace"])
def test_run_salt_that_would_break_base_sub_is_rejected(client, run_salt):
    response = client.post("/auth/demo-login", json={"sub": "mallory.x", "run_salt": run_salt})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_run_salt_is_not_validated_for_unsalted_personas(client):
    response = client.post("/auth/demo-login", json={"sub": "alex.rivera", "run_salt": "has-dash"})
    assert response.status_code == 200
    assert response.json()["sub"] == "alex.rivera"


@pytest.mark.parametrize("body", [None, [], {"sub": ""}, {"sub": 7}, {"sub": "mallory.x", "run_salt": 5}])
def test_demo_login_bad_body_is_invalid_request(client, body):
    if body is None:
        response = client.post("/auth/demo-login", content=b"not json", headers={"content-type": "application/json"})
    else:
        response = client.post("/auth/demo-login", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_secret_check_uses_hmac_compare_digest_even_for_unknown_clients(client, monkeypatch):
    calls = []
    real_compare = core.hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real_compare(a, b)

    monkeypatch.setattr(core.hmac, "compare_digest", spy)
    assert client_credentials(client, "agent-shopper", "copperline-tools").status_code == 200
    assert client_credentials(client, "agent-shopper", "copperline-tools", secret="wrong").status_code == 401
    response = client.post("/auth/token", data={"grant_type": "client_credentials", "client_id": "nobody",
                                                "client_secret": "x", "audience": "copperline-tools"})
    assert response.status_code == 401
    assert calls == [(bytes, bytes)] * 3


def test_secrets_fall_back_to_per_client_env_when_json_is_not_set(flat_config, pem, monkeypatch):
    monkeypatch.setenv("AGENT_SHOPPER_SECRET", "from-env-0123456789")
    monkeypatch.delenv("AGENT_STOREOPS_SECRET", raising=False)
    settings = Settings(config_dir=flat_config, issuer=ISSUER, signing_key_pem=pem, client_secrets=None)
    with TestClient(create_app(settings)) as test_client:
        ok = client_credentials(test_client, "agent-shopper", "copperline-tools", secret="from-env-0123456789")
        assert ok.status_code == 200
        unset = client_credentials(test_client, "agent-storeops", "copperline-tools", secret="")
        assert unset.status_code == 401


def test_repo_layout_matrix_under_policy_is_found(pem):
    settings = Settings(config_dir=REPO_CONFIG, issuer=ISSUER, signing_key_pem=pem, client_secrets=dict(SECRETS))
    with TestClient(create_app(settings)) as test_client:
        assert exchange(test_client, "agent-shopper", login(test_client, "alex.rivera")).status_code == 200


def test_missing_matrix_fails_at_startup(tmp_path, pem):
    shutil.copy(REPO_CONFIG / "personas.yaml", tmp_path / "personas.yaml")
    settings = Settings(config_dir=tmp_path, issuer=ISSUER, signing_key_pem=pem, client_secrets={})
    with pytest.raises(FileNotFoundError, match="tool-access-matrix.yaml"):
        create_app(settings)


def test_agent_with_empty_allowed_roles_cannot_exchange(tmp_path, pem):
    shutil.copy(REPO_CONFIG / "personas.yaml", tmp_path / "personas.yaml")
    matrix = yaml.safe_load((REPO_CONFIG / "policy" / "tool-access-matrix.yaml").read_text())
    matrix["agents"]["shopper"]["allowed_user_roles"] = []
    (tmp_path / "tool-access-matrix.yaml").write_text(yaml.safe_dump(matrix))
    settings = Settings(config_dir=tmp_path, issuer=ISSUER, signing_key_pem=pem, client_secrets=dict(SECRETS))
    with TestClient(create_app(settings)) as test_client:
        response = exchange(test_client, "agent-shopper", login(test_client, "alex.rivera"))
        assert response.json()["error"] == "unauthorized_user_role"


def test_partner_token_as_subject_is_unauthorized_user_role(client):
    # README: a partner token passes the "user token" check (no agent_id / svc_role) but no agent allows "partner".
    partner = client_credentials(client, "granite-supply", "copperline-partner").json()["access_token"]
    response = exchange(client, "agent-shopper", partner)
    assert response.status_code == 400
    assert response.json()["error"] == "unauthorized_user_role"


def test_unknown_sub_is_404_before_run_salt_is_checked(client):
    response = client.post("/auth/demo-login", json={"sub": "nobody", "run_salt": "has-dash"})
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_user"


def test_subject_token_without_role_is_invalid_grant(client, key):
    subject = sign({"sub": "alex.rivera", "aud": "copperline-shop"}, key, ttl_s=60, issuer=ISSUER)
    response = exchange(client, "agent-shopper", subject)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_jwks_matches_the_signing_key(client, key):
    assert client.get("/.well-known/jwks.json").json() == public_jwks(key)


def test_token_responses_are_not_cacheable(client):
    response = client_credentials(client, "agent-shopper", "copperline-tools")
    assert response.headers["cache-control"] == "no-store"
    refused = client_credentials(client, "agent-shopper", "copperline-tools", secret="wrong")
    assert refused.headers["cache-control"] == "no-store"


def test_wrong_method_is_json_405(client):
    response = client.get("/auth/token")
    assert response.status_code == 405
    assert response.json()["error"] == "method_not_allowed"
    assert response.headers["x-copperline-origin"] == "idp"


def test_json_body_on_a_form_endpoint_is_invalid_request(client):
    response = client.post("/auth/token", json={"grant_type": "client_credentials", "client_id": "agent-shopper"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_issue_and_refusal_logs_name_the_grant_and_never_the_secret(client, capsys):
    capsys.readouterr()
    exchange(client, "agent-shopper", login(client, "alex.rivera"))
    client_credentials(client, "agent-refunds", "copperline-tools")
    client_credentials(client, "agent-refunds", "copperline-erp")
    records = log_records(capsys)

    issued = [r for r in records if r["event"] == "token_issued"]
    assert [(r["grant"], r["client_id"]) for r in issued] == [
        ("demo_login", None),
        ("token_exchange", "agent-shopper"),
        ("client_credentials", "agent-refunds"),
    ]
    assert issued[2]["obo_sub"] == issued[2]["obo_role"] == "none"
    assert all(r["service"] == "idp" and r["status"] == 200 for r in issued)

    (refused,) = [r for r in records if r["event"] == "token_refused"]
    assert (refused["grant"], refused["client_id"], refused["error"], refused["status"]) == (
        "client_credentials", "agent-refunds", "invalid_target", 400
    )
    text = json.dumps(records)
    assert not [secret for secret in SECRETS.values() if secret in text]
    assert "access_token" not in text
