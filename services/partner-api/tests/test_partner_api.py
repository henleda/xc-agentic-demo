"""partner-api contract tests (services/partner-api/README.md, deploy/xc/openapi/partner-api.yaml)."""

import json
from pathlib import Path

import pytest
import yaml
from copperline_common.jwt import public_jwks, sign
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from partner_api import data
from partner_api.app import create_app
from partner_api.settings import Settings

REPO = Path(__file__).resolve().parents[3]
ISSUER = "https://idp.copperline.demo"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SPEC = yaml.safe_load((REPO / "deploy/xc/openapi/partner-api.yaml").read_text())


def token(**overrides) -> str:
    claims = {"sub": "granite-supply", "role": "partner", "aud": "copperline-partner"}
    claims.update(overrides)
    return sign(claims, KEY, ttl_s=300, issuer=overrides.pop("iss", ISSUER))


@pytest.fixture
def client(tmp_path):
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps(public_jwks(KEY)))
    with TestClient(create_app(Settings(jwks_path=str(jwks)))) as test_client:
        yield test_client


def get(client, path, bearer=None, **kwargs):
    headers = {"Authorization": f"Bearer {bearer or token()}"} if bearer != "" else {}
    return client.get(path, headers=headers, **kwargs)


# ---- auth: re-checked here even though XC checks it first ------------------------------------------------------------


@pytest.mark.parametrize(
    "bearer, status",
    [
        ("", 401),
        ("not-a-jwt", 401),
        (None, 200),  # the default partner token
    ],
)
def test_a_partner_token_is_required(client, bearer, status):
    assert get(client, "/v1/purchase-orders", bearer).status_code == status


@pytest.mark.parametrize("overrides", [{"aud": "copperline-shop"}, {"iss": "https://evil.test"}])
def test_wrong_audience_or_issuer_is_401(client, overrides):
    response = get(client, "/v1/purchase-orders", token(**overrides))
    assert response.status_code == 401 and response.headers["x-copperline-origin"] == "partner-api"


def test_a_token_signed_by_another_key_is_401(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = sign({"sub": "granite-supply", "role": "partner", "aud": "copperline-partner"}, other, ttl_s=300,
                  issuer=ISSUER)
    assert get(client, "/v1/purchase-orders", forged).status_code == 401


def test_a_valid_token_without_the_partner_role_is_403(client):
    assert get(client, "/v1/purchase-orders", token(role="customer")).status_code == 403


def test_auth_runs_before_routing(client):
    """No token: 401 even for paths that do not exist, so an anonymous caller learns nothing about the API."""
    assert get(client, "/v1/nope", "").status_code == 401
    assert get(client, "/v1/internal/cost-sheet", "").status_code == 401


def test_healthz_needs_no_token(client):
    assert client.get("/healthz").json() == {"status": "ok", "service": "partner-api"}


# ---- documented operations -----------------------------------------------------------------------------------------


def test_purchase_orders_are_paged_25_at_a_time_and_deterministic(client):
    first = get(client, "/v1/purchase-orders").json()
    again = get(client, "/v1/purchase-orders?page=1").json()
    assert first == again and first["page_size"] == 25 and len(first["items"]) == 25
    assert first["total"] == data.PO_COUNT
    assert get(client, "/v1/purchase-orders?page=2").json()["items"][0]["po_id"] == "PO-5026"


@pytest.mark.parametrize("page", [1, 50, 100])  # S6(d) reads pages 1..50; the spec allows 1..100
def test_every_page_the_spec_allows_is_200(client, page):
    assert get(client, f"/v1/purchase-orders?page={page}").status_code == 200


@pytest.mark.parametrize("query", ["page=0", "page=101", "page=x", "status=lost"])
def test_out_of_spec_queries_are_422(client, query):
    assert get(client, f"/v1/purchase-orders?{query}").status_code == 422


def test_status_filter(client):
    items = get(client, "/v1/purchase-orders?status=open").json()["items"]
    assert items and {po["status"] for po in items} == {"open"}


def test_one_purchase_order(client):
    assert get(client, "/v1/purchase-orders/PO-5001").json()["po_id"] == "PO-5001"
    assert get(client, "/v1/purchase-orders/PO-9999").status_code == 404
    assert get(client, "/v1/purchase-orders/PO-50011").status_code == 404  # outside the spec pattern


def test_asn_is_202_and_echoes_po_id_even_for_the_s6_injection_string(client):
    """The WAF is meant to stop S6(c); if it does not, the service just stores a string. No SQL anywhere."""
    body = {"po_id": "PO-5531' OR '1'='1", "carrier": "UPS", "items": []}
    response = client.post("/v1/asn", json=body, headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 202
    assert response.json() == {"asn_id": "ASN-00001", "po_id": "PO-5531' OR '1'='1", "status": "accepted"}


@pytest.mark.parametrize("body", [{}, {"po_id": "PO-5001", "carrier": "x" * 33, "items": []},
                                  {"po_id": "PO-5001", "carrier": "UPS", "items": "nope"}])
def test_bad_asn_bodies_are_422(client, body):
    response = client.post("/v1/asn", json=body, headers={"Authorization": f"Bearer {token()}"})
    assert response.status_code == 422


# ---- shadow operations -----------------------------------------------------------------------------------------------


def test_the_shadow_endpoints_exist_and_carry_sensitive_looking_data(client):
    """They must really answer (XC API discovery has to see them) and hold something sensitive-data discovery flags."""
    cost = get(client, "/v1/internal/cost-sheet").json()
    assert cost["remit_to"]["iban"].startswith("DE89") and "@" in cost["contact"]["email"]
    assert get(client, "/v1/internal/margins").json()["by_category"]


def test_the_published_spec_documents_exactly_the_three_operations_and_no_shadow_path():
    operations = {(path, method) for path, ops in SPEC["paths"].items() for method in ops}
    assert operations == {("/v1/purchase-orders", "get"), ("/v1/purchase-orders/{po_id}", "get"),
                          ("/v1/asn", "post")}


def test_the_app_implements_every_documented_operation_and_both_shadow_paths(client):
    routes = {(route.path, method.lower()) for route in client.app.routes for method in getattr(route, "methods", ())}
    assert {("/v1/purchase-orders", "get"), ("/v1/purchase-orders/{po_id}", "get"), ("/v1/asn", "post"),
            ("/v1/internal/cost-sheet", "get"), ("/v1/internal/margins", "get")} <= routes


def test_settings_refuse_to_start_without_keys(monkeypatch):
    monkeypatch.delenv("JWKS_PATH", raising=False)
    monkeypatch.delenv("IDP_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="JWKS_PATH"):
        Settings.from_env()
