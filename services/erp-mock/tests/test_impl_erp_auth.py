"""Business-API auth (README "Auth"): a service token with aud copperline-erp and svc_role=tool-server, or nothing.

401 = no token or a token that fails verification; 403 = a valid token that is not a tool-server service token.
"""

import pytest
from erp_mock.app import create_business_app
from erp_mock.db import Db
from erp_mock.settings import Settings
from fastapi.testclient import TestClient

ISSUER = "https://idp.copperline.demo"  # same as conftest.py
SERVICE_CLAIMS = {"sub": "mcp-servers", "svc_role": "tool-server", "aud": "copperline-erp"}
ORDER = "/orders/CO-1042"


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def assert_denied(response, status: int, error: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == error
    assert isinstance(body["detail"], str) and body["detail"]
    assert "4111111111111111" not in response.text


def test_valid_service_token_is_accepted(business, auth):
    assert business.get(ORDER, headers=auth).status_code == 200


def test_healthz_needs_no_token(business):
    response = business.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "erp-mock"}


def test_no_token_is_401(business):
    assert_denied(business.get(ORDER), 401, "unauthorized")


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer    ", "Token abc", "not-a-jwt"])
def test_malformed_authorization_is_401(business, header):
    assert_denied(business.get(ORDER, headers={"Authorization": header}), 401, "unauthorized")


def test_garbage_bearer_is_401(business):
    assert_denied(business.get(ORDER, headers=bearer("abc.def.ghi")), 401, "unauthorized")


def test_wrong_audience_is_401(business, make_token):
    token = make_token(**{**SERVICE_CLAIMS, "aud": "copperline-tools"})
    assert_denied(business.get(ORDER, headers=bearer(token)), 401, "unauthorized")


def test_missing_audience_is_401(business, make_token):
    token = make_token(sub="mcp-servers", svc_role="tool-server")
    assert_denied(business.get(ORDER, headers=bearer(token)), 401, "unauthorized")


def test_wrong_issuer_is_401(business, make_token):
    token = make_token(issuer="https://evil.example", **SERVICE_CLAIMS)
    assert_denied(business.get(ORDER, headers=bearer(token)), 401, "unauthorized")


def test_expired_token_is_401(business, make_token):
    token = make_token(ttl_s=-60, **SERVICE_CLAIMS)
    assert_denied(business.get(ORDER, headers=bearer(token)), 401, "unauthorized")


def test_token_from_another_key_is_401(business, make_token, other_rsa_key):
    token = make_token(key=other_rsa_key, **SERVICE_CLAIMS)
    assert_denied(business.get(ORDER, headers=bearer(token)), 401, "unauthorized")


def test_valid_token_without_svc_role_is_403(business, make_token):
    token = make_token(sub="granite-supply", role="partner", aud="copperline-erp")
    assert_denied(business.get(ORDER, headers=bearer(token)), 403, "forbidden")


def test_agent_token_with_erp_audience_but_no_svc_role_is_403(business, make_token):
    """An agent that somehow got aud copperline-erp is still not a tool server (no hopping straight to the ERP)."""
    token = make_token(
        sub="refunds", agent_id="refunds", agent_role="refunds", obo_sub="morgan.chen", obo_role="care_supervisor",
        aud="copperline-erp",
    )
    assert_denied(business.get(ORDER, headers=bearer(token)), 403, "forbidden")


def test_other_svc_role_is_403(business, make_token):
    token = make_token(**{**SERVICE_CLAIMS, "svc_role": "batch"})
    assert_denied(business.get(ORDER, headers=bearer(token)), 403, "forbidden")


def test_audience_list_containing_erp_is_accepted(business, make_token):
    token = make_token(**{**SERVICE_CLAIMS, "aud": ["copperline-tools", "copperline-erp"]})
    assert business.get(ORDER, headers=bearer(token)).status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/orders/CO-1042", None),
        ("GET", "/customers/*/orders", None),
        ("GET", "/inventory/TNT-ALPINE-2", None),
        ("POST", "/inventory/adjust", {"sku": "TNT-ALPINE-2", "store_id": "S-001", "delta": 1}),
        ("POST", "/inventory/transfer", {"sku": "TNT-ALPINE-2", "from_store": "S-001", "to_store": "S-002", "qty": 1}),
        ("POST", "/refunds", {"order_id": "CO-1042", "amount": 1}),
        ("GET", "/refunds/RF-0001", None),
    ],
)
def test_every_business_route_needs_a_token(business, admin, method, path, body):
    assert_denied(business.request(method, path, json=body), 401, "unauthorized")
    assert admin.get("/admin/state").json()["refunds_issued"] == 0
    assert admin.get("/admin/state").json()["stock_writes"] == 0


def test_auth_runs_before_routing_and_validation(business, make_token):
    """No token: 401 even for an unknown path or a bad body, so an unauthenticated caller learns nothing."""
    assert business.get("/no/such/route").status_code == 401
    assert business.post("/refunds", json={"amount": "lots"}).status_code == 401
    forbidden = bearer(make_token(sub="granite-supply", role="partner", aud="copperline-erp"))
    assert business.post("/refunds", json={"amount": "lots"}, headers=forbidden).status_code == 403


def test_unreadable_jwks_is_503_not_401(tmp_path, make_token):
    """Our missing key file is our fault: say so, instead of blaming the caller's token."""
    settings = Settings(db_path=tmp_path / "erp.sqlite3", issuer=ISSUER, jwks_path=str(tmp_path / "missing.json"))
    db = Db(settings.db_path)
    with TestClient(create_business_app(settings, db)) as client:
        response = client.get(ORDER, headers=bearer(make_token()))
    db.close()
    assert response.status_code == 503
    assert response.json()["error"] == "unavailable"
    assert "jwks" in response.json()["detail"]


def test_denials_are_logged_without_the_token(business, make_token, read_logs):
    read_logs()
    token = make_token(sub="granite-supply", role="partner", aud="copperline-erp")
    business.get(ORDER, headers=bearer(token))
    records, text = read_logs()
    deny = [r for r in records if r["event"] == "erp_auth_deny"]
    assert len(deny) == 1 and deny[0]["status"] == 403 and deny[0]["path"] == ORDER
    assert token not in text
    request = [r for r in records if r["event"] == "http_request"]
    assert request and request[0]["status"] == 403 and request[0]["request_id"]
