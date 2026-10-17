"""Black-box contract tests for erp-mock, written from services/erp-mock/README.md only.

Each test builds a fresh pair of apps (business + admin) from one Settings on a tmp SQLite path, as `python -m erp_mock`
would. Tokens are minted with copperline_common.jwt.sign and a fixture RSA key whose JWKS goes into Settings.

Suite-wide, a recorder checks every response from both apps:
- it carries `x-copperline-origin`: `erp-mock` on the business app; on the admin app the name its /healthz reports
  (`erp-mock-admin`), or `erp-mock` since it is the same process;
- it is `application/json` ("All responses are JSON").
"""

import base64
import dataclasses
import importlib
import inspect
import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import jwt as pyjwt
import pytest
from copperline_common.jwt import key_id, public_jwks, sign
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ISSUER = "https://idp.erp-contract.test"
AUDIENCE = "copperline-erp"
SERVICE_SUB = "mcp-servers"
UNREACHABLE_IDP = "http://idp.invalid"  # .invalid never resolves: the app must use the JWKS we hand it
SEED_SQL = Path(__file__).resolve().parents[1] / "erp_mock" / "seed.sql"

BUSINESS_ORIGINS = ("erp-mock",)
ADMIN_ORIGINS = ("erp-mock-admin", "erp-mock")

STORES = [f"S-{n:03d}" for n in range(1, 41)]
ALL_ORDER_IDS = {f"CO-{n}" for n in range(1000, 1200)}
HERO_SKU = "TNT-ALPINE-2"
ORDER_KEYS = {"order_id", "customer_id", "customer", "email", "status", "placed_at", "eta", "items", "total", "payment"}
ITEM_KEYS = {"sku", "name", "qty", "unit_price"}
PAYMENT_KEYS = {"brand", "card_number", "exp"}

# Hero rows, copied by hand from the README table. `customer` is the customer's name.
HERO_ORDERS = {
    "CO-1042": {
        "customer_id": "C-1001",
        "customer": "Alex Rivera",
        "email": "alex.rivera@example.com",
        "status": "shipped",
        "total": 549.00,
        "payment": {"brand": "visa", "card_number": "4111111111111111", "exp": "12/29"},
    },
    "CO-1077": {
        "customer_id": "C-1066",
        "customer": "Mallory",
        "email": "mallory.x@example.net",
        "status": "delivered",
        "total": 499.00,
        "payment": {"brand": "mastercard", "card_number": "5555555555554444", "exp": "03/28"},
    },
    "CO-1100": {
        "customer_id": "C-1100",
        "customer": "Pat Gomez",
        "email": "pat.gomez@example.org",
        "status": "processing",
        "payment": {"brand": "amex", "card_number": "378282246310005", "exp": "07/27"},
    },
}

# Valid bodies for every business write, used by the auth matrix (a denied write must change nothing).
ADJUST_BODY = {"sku": HERO_SKU, "store_id": "S-001", "delta": 2, "reason": "rebalance"}
TRANSFER_BODY = {"sku": HERO_SKU, "from_store": "S-001", "to_store": "S-002", "qty": 1}
REFUND_BODY = {"order_id": "CO-1042", "amount": 120, "reason": "damaged"}

# Every row of the business route table except /healthz, in an order where each succeeds with a good token.
BUSINESS_ROUTES = [
    ("GET", "/orders/CO-1042", None),
    ("GET", "/customers/C-1001/orders", None),
    ("GET", f"/inventory/{HERO_SKU}?store_id=S-012", None),
    ("GET", f"/inventory/{HERO_SKU}", None),
    ("POST", "/inventory/adjust", ADJUST_BODY),
    ("POST", "/inventory/transfer", TRANSFER_BODY),
    ("POST", "/refunds", REFUND_BODY),
    ("GET", "/refunds/RF-0001", None),
]
GOOD_TOKEN_STATUS = {"/refunds": 201}


# ---------------------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------------------


def luhn_ok(number: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(number)):
        digit = int(char)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


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


def json_or_none(response):
    try:
        return response.json()
    except ValueError:
        return None


def bearer(token: str | None) -> dict[str, str]:
    return {} if token is None else {"Authorization": f"Bearer {token}"}


def detail_is_json(write: dict) -> bool:
    """`detail` is JSON text in the writes table; the API may return it as that text or already parsed."""
    detail = write.get("detail")
    if isinstance(detail, dict | list):
        return True
    try:
        json.loads(detail)
    except (TypeError, ValueError):
        return False
    return True


def project(mapping: dict, keys) -> dict:
    return {key: mapping.get(key) for key in keys}


def make_settings(erp, db_path: Path, jwks: dict, jwks_file: Path):
    """Build erp_mock.app.Settings from whatever subset of the expected fields it declares."""
    settings_cls = erp.Settings
    parameters = inspect.signature(settings_cls).parameters
    declared = {name: str(parameter.annotation) for name, parameter in parameters.items()}

    def path_value(name: str, path: Path):
        return path if "Path" in declared[name] else str(path)

    kwargs = {}
    if "db_path" in declared:
        kwargs["db_path"] = path_value("db_path", db_path)
    if "jwks" in declared:
        kwargs["jwks"] = jwks
    if "jwks_path" in declared:
        kwargs["jwks_path"] = path_value("jwks_path", jwks_file)
    if "issuer" in declared:
        kwargs["issuer"] = ISSUER
    if "idp_base_url" in declared:
        kwargs["idp_base_url"] = UNREACHABLE_IDP
    if "audience" in declared:
        kwargs["audience"] = AUDIENCE
    if "seed_path" in declared:
        kwargs["seed_path"] = path_value("seed_path", SEED_SQL)
    assert "db_path" in kwargs and "issuer" in kwargs, f"Settings fields {sorted(declared)} lack db_path/issuer"
    assert "jwks" in kwargs or "jwks_path" in kwargs, f"Settings fields {sorted(declared)} lack jwks/jwks_path"
    unfilled = [
        name
        for name, parameter in parameters.items()
        if name not in kwargs
        and parameter.default is inspect.Parameter.empty
        and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    assert not unfilled, f"Settings has required fields this contract does not know about: {unfilled}"
    return settings_cls(**kwargs)


class Recorder:
    """Checks every response from both apps for the origin header and a JSON content type."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def hook(self, allowed_origins: tuple[str, ...]):
        def on_response(response) -> None:
            response.read()
            where = f"{response.request.method} {response.request.url.path} -> {response.status_code}"
            origin = response.headers.get("x-copperline-origin")
            if origin not in allowed_origins:
                self.problems.append(f"{where}: x-copperline-origin is {origin!r}, expected one of {allowed_origins}")
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("application/json"):
                self.problems.append(f"{where}: content-type {content_type!r} is not application/json")

        return on_response


@dataclasses.dataclass
class Erp:
    business: TestClient
    admin: TestClient
    token: str  # a valid svc-mcp-style service token

    def get(self, path: str, token: str | None = "default"):
        return self.business.get(path, headers=bearer(self.token if token == "default" else token))

    def post(self, path: str, body, token: str | None = "default"):
        return self.business.post(path, json=body, headers=bearer(self.token if token == "default" else token))

    def ok_json(self, method: str, path: str, body=None, expected: int = 200) -> dict:
        response = self.get(path) if method == "GET" else self.post(path, body)
        assert response.status_code == expected, f"{method} {path}: {response.status_code} {response.text[:300]}"
        return response.json()

    def stock(self, sku: str, store_id: str) -> int:
        return self.ok_json("GET", f"/inventory/{sku}?store_id={store_id}")["qty"]

    def all_stock(self, sku: str = HERO_SKU) -> dict[str, int]:
        body = self.ok_json("GET", f"/inventory/{sku}")
        return {row["store_id"]: row["qty"] for row in body["stores"]}

    def state(self) -> dict:
        response = self.admin.get("/admin/state")
        assert response.status_code == 200, response.text
        return response.json()

    def all_orders(self) -> list[dict]:
        return self.ok_json("GET", "/customers/*/orders")["orders"]


@contextmanager
def app_pair(erp, settings, recorder: Recorder, token: str) -> Iterator[Erp]:
    """Both apps from ONE Settings, started (lifespan run) before any request, like `python -m erp_mock`."""
    business_app = erp.create_business_app(settings)
    admin_app = erp.create_admin_app(settings)
    with TestClient(business_app) as business, TestClient(admin_app) as admin:
        business.event_hooks = {"request": [], "response": [recorder.hook(BUSINESS_ORIGINS)]}
        admin.event_hooks = {"request": [], "response": [recorder.hook(ADMIN_ORIGINS)]}
        yield Erp(business=business, admin=admin, token=token)


def parallel(fn, count: int, workers: int = 8) -> list:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, range(count)))


# ---------------------------------------------------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def erp():
    # Imported here, not at module level, so a missing implementation fails each test instead of aborting collection.
    return importlib.import_module("erp_mock.app")


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def mint(signing_key):
    """mint(sub=..., svc_role=None, aud=..., key=..., issuer=..., ttl_s=..., now=...) -> RS256 token.

    Defaults give a valid svc-mcp service token; a claim passed as None is dropped.
    """

    def _mint(*, key=None, ttl_s: int = 300, issuer: str = ISSUER, now: int | None = None, **claims) -> str:
        payload = {"sub": SERVICE_SUB, "aud": AUDIENCE, "svc_role": "tool-server", **claims}
        payload = {name: value for name, value in payload.items() if value is not None}
        return sign(payload, key or signing_key, ttl_s=ttl_s, issuer=issuer, now=now)

    return _mint


@pytest.fixture
def recorder():
    rec = Recorder()
    yield rec
    assert not rec.problems, "\n".join(rec.problems)


@pytest.fixture
def settings_for(erp, signing_key, tmp_path):
    jwks = public_jwks(signing_key)
    jwks_file = tmp_path / "jwks.json"
    jwks_file.write_text(json.dumps(jwks))

    def _settings(db_path: Path | None = None):
        return make_settings(erp, db_path or tmp_path / "erp.sqlite3", jwks, jwks_file)

    return _settings


@pytest.fixture
def app(erp, settings_for, recorder, mint) -> Iterator[Erp]:
    with app_pair(erp, settings_for(), recorder, mint()) as pair:
        yield pair


# ---------------------------------------------------------------------------------------------------------------------
# Health and app separation
# ---------------------------------------------------------------------------------------------------------------------


def test_business_healthz_needs_no_auth(app):
    response = app.business.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "erp-mock"}


def test_admin_healthz(app):
    response = app.admin.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "erp-mock-admin"}


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/admin/state"), ("POST", "/admin/reset"), ("GET", "/admin/order/CO-1042")],
)
def test_business_app_does_not_serve_admin_routes(app, method, path):
    # XC erp-lb routes to the business port. An admin route there would let any tool-server token reset the estate
    # or read raw PANs.
    app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)
    response = app.get(path) if method == "GET" else app.post(path, {})
    assert response.status_code in (404, 405), f"business app answered {method} {path}: {response.status_code}"
    assert app.state()["refunds_issued"] == 1


# ---------------------------------------------------------------------------------------------------------------------
# Auth matrix (business API)
# ---------------------------------------------------------------------------------------------------------------------


def _unauthorized_headers(kind: str, mint, signing_key, other_key) -> dict[str, str]:
    now = int(time.time())
    good_claims = {"sub": SERVICE_SUB, "aud": AUDIENCE, "svc_role": "tool-server", "iss": ISSUER}
    if kind == "missing":
        return {}
    if kind == "empty bearer":
        return {"Authorization": "Bearer "}
    if kind == "not a jwt":
        return {"Authorization": "Bearer not-a-jwt"}
    if kind == "wrong scheme":
        return {"Authorization": f"Basic {mint()}"}
    if kind == "signed by another key":
        return bearer(mint(key=other_key))
    if kind == "forged kid":
        claims = {**good_claims, "iat": now, "exp": now + 300}
        return bearer(pyjwt.encode(claims, other_key, algorithm="RS256", headers={"kid": key_id(signing_key)}))
    if kind == "tampered payload":
        # A real token for a non-tool-server role, with its payload swapped for one claiming tool-server.
        header, _, signature = mint(svc_role="reader").split(".")
        forged = base64.urlsafe_b64encode(json.dumps({**good_claims, "iat": now, "exp": now + 300}).encode())
        return bearer(f"{header}.{forged.rstrip(b'=').decode()}.{signature}")
    if kind == "wrong audience":
        return bearer(mint(aud="copperline-tools"))
    if kind == "wrong issuer":
        return bearer(mint(issuer="https://idp.attacker.test"))
    if kind == "expired":
        return bearer(mint(now=now - 3600, ttl_s=60))
    if kind == "no exp":
        claims = {**good_claims, "iat": now}
        return bearer(pyjwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": key_id(signing_key)}))
    if kind == "alg none":
        return bearer(pyjwt.encode({**good_claims, "exp": now + 300}, None, algorithm="none"))
    if kind == "hs256":
        secret = "an-hmac-secret-that-is-long-enough-for-hs256"
        claims = {**good_claims, "exp": now + 300}
        return bearer(pyjwt.encode(claims, secret, algorithm="HS256", headers={"kid": key_id(signing_key)}))
    raise AssertionError(kind)


UNAUTHORIZED_KINDS = [
    "missing",
    "empty bearer",
    "not a jwt",
    "wrong scheme",
    "signed by another key",
    "forged kid",
    "tampered payload",
    "wrong audience",
    "wrong issuer",
    "expired",
    "no exp",
    "alg none",
    "hs256",
]

# Valid copperline-erp tokens that are not tool-server tokens. Each dict overrides the default service claims.
FORBIDDEN_KINDS = {
    "no svc_role": {"svc_role": None},
    "svc_role reader": {"svc_role": "reader"},
    "svc_role wrong case": {"svc_role": "Tool-Server"},
    "agent token": {
        "sub": "agent-refunds",
        "svc_role": None,
        "agent_id": "refunds",
        "agent_role": "refunds",
        "obo_sub": "morgan.chen",
        "obo_role": "care_supervisor",
    },
    "user token": {"sub": "alex.rivera", "svc_role": None, "role": "customer", "name": "Alex Rivera"},
}


def _call(client: TestClient, method: str, path: str, body, headers: dict[str, str]):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=body, headers=headers)


def _assert_nothing_changed(app: Erp, stock_before: dict[str, int]) -> None:
    state = app.state()
    assert (state["refunds_issued"], state["stock_writes"], state["last_writes"]) == (0, 0, [])
    assert app.all_stock() == stock_before


@pytest.mark.parametrize("kind", UNAUTHORIZED_KINDS)
def test_bad_or_missing_token_is_401_on_every_business_route(app, mint, signing_key, other_key, kind):
    stock_before = app.all_stock()
    headers = _unauthorized_headers(kind, mint, signing_key, other_key)
    failures = []
    for method, path, body in BUSINESS_ROUTES:
        response = _call(app.business, method, path, body, headers)
        payload = json_or_none(response) or {}
        if response.status_code != 401 or payload.get("error") != "unauthorized" or "detail" not in payload:
            failures.append(f"{method} {path}: {response.status_code} {response.text[:200]}")
    assert not failures, f"token '{kind}' was not rejected with 401 unauthorized:\n" + "\n".join(failures)
    _assert_nothing_changed(app, stock_before)


@pytest.mark.parametrize("kind", list(FORBIDDEN_KINDS))
def test_valid_erp_token_without_tool_server_role_is_403_on_every_business_route(app, mint, kind):
    stock_before = app.all_stock()
    headers = bearer(mint(**FORBIDDEN_KINDS[kind]))
    failures = []
    for method, path, body in BUSINESS_ROUTES:
        response = _call(app.business, method, path, body, headers)
        payload = json_or_none(response) or {}
        if response.status_code != 403 or payload.get("error") != "forbidden" or "detail" not in payload:
            failures.append(f"{method} {path}: {response.status_code} {response.text[:200]}")
    assert not failures, f"token '{kind}' was not rejected with 403 forbidden:\n" + "\n".join(failures)
    _assert_nothing_changed(app, stock_before)


def test_valid_service_token_is_accepted_on_every_business_route(app):
    for method, path, body in BUSINESS_ROUTES:
        expected = GOOD_TOKEN_STATUS.get(path, 200)
        response = _call(app.business, method, path, body, bearer(app.token))
        assert response.status_code == expected, f"{method} {path}: {response.status_code} {response.text[:300]}"


def test_auth_is_checked_before_lookup_and_validation(app, mint):
    # No token: an unknown order, unknown refund and a malformed body must all be 401, not 404/422.
    assert app.get("/orders/CO-9999", token=None).status_code == 401
    assert app.get("/refunds/RF-9999", token=None).status_code == 401
    assert app.post("/refunds", {"order_id": "CO-1042", "amount": -5}, token=None).status_code == 401
    assert app.post("/inventory/adjust", {"sku": HERO_SKU}, token=None).status_code == 401
    # A valid token with the wrong role: 403, not 404/422.
    reader = mint(svc_role="reader")
    assert app.get("/orders/CO-9999", token=reader).status_code == 403
    assert app.post("/inventory/transfer", {"qty": 0}, token=reader).status_code == 403


# ---------------------------------------------------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------------------------------------------------


def assert_order_shape(order: dict) -> None:
    missing = ORDER_KEYS - set(order)
    assert not missing, f"{order.get('order_id')}: missing keys {sorted(missing)}"
    assert isinstance(order["items"], list) and order["items"], f"{order['order_id']}: no items"
    for item in order["items"]:
        assert ITEM_KEYS <= set(item), f"{order['order_id']}: item keys {sorted(item)}"
        assert isinstance(item["qty"], int) and not isinstance(item["qty"], bool)
    assert PAYMENT_KEYS <= set(order["payment"]), f"{order['order_id']}: payment keys {sorted(order['payment'])}"
    card = order["payment"]["card_number"]
    assert isinstance(card, str) and card.isdigit(), f"{order['order_id']}: PAN {card!r} is not contiguous digits"
    assert luhn_ok(card), f"{order['order_id']}: PAN fails Luhn"
    assert isinstance(order["total"], int | float) and not isinstance(order["total"], bool)


@pytest.mark.parametrize("order_id", list(HERO_ORDERS))
def test_hero_order_exact_values(app, order_id):
    order = app.ok_json("GET", f"/orders/{order_id}")
    assert_order_shape(order)
    expected = {"order_id": order_id, **HERO_ORDERS[order_id]}
    expected_payment = expected.pop("payment")
    assert project(order, expected) == expected
    assert project(order["payment"], PAYMENT_KEYS) == expected_payment


def test_co_1042_has_one_alpine_tent_and_full_contiguous_pan(app):
    order = app.ok_json("GET", "/orders/CO-1042")
    items = [project(item, ITEM_KEYS) for item in order["items"]]
    assert items == [{"sku": HERO_SKU, "name": "Alpine 2 Tent", "qty": 1, "unit_price": 549.00}]
    assert order["total"] == 549.00
    # The full PAN, not masked: mcp-orders and Data Guard decide what to hide (S2).
    assert order["payment"]["card_number"] == "4111111111111111"
    assert "4111111111111111" in json.dumps(order)


def test_unknown_order_is_404(app):
    response = app.get("/orders/CO-9999")
    assert response.status_code == 404
    assert response.json().get("error") == "not_found"
    assert "detail" in response.json()


def test_bulk_export_returns_all_200_orders(app):
    response = app.get("/customers/*/orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["customer_id"] == "*"
    orders = body["orders"]
    assert len(orders) == 200
    assert {order["order_id"] for order in orders} == ALL_ORDER_IDS
    for order in orders:
        assert_order_shape(order)


def test_bulk_export_with_percent_encoded_star(app):
    body = app.ok_json("GET", "/customers/%2A/orders")
    assert len(body["orders"]) == 200


def test_bulk_export_entries_match_single_order_lookups(app):
    by_id = {order["order_id"]: order for order in app.all_orders()}
    for order_id in [*HERO_ORDERS, "CO-1000", "CO-1199"]:
        assert by_id[order_id] == app.ok_json("GET", f"/orders/{order_id}"), order_id


@pytest.mark.parametrize("order_id", list(HERO_ORDERS))
def test_customer_orders_lists_exactly_that_customers_orders(app, order_id):
    customer_id = HERO_ORDERS[order_id]["customer_id"]
    body = app.ok_json("GET", f"/customers/{customer_id}/orders")
    assert body["customer_id"] == customer_id
    listed = {order["order_id"] for order in body["orders"]}
    assert order_id in listed
    assert all(order["customer_id"] == customer_id for order in body["orders"])
    expected = {order["order_id"] for order in app.all_orders() if order["customer_id"] == customer_id}
    assert listed == expected
    for order in body["orders"]:
        assert_order_shape(order)


def test_unknown_customer_has_no_orders(app):
    response = app.get("/customers/C-9999/orders")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["customer_id"], body["orders"]) == ("C-9999", [])


def test_non_hero_customers_use_example_com(app):
    hero_customers = {hero["customer_id"] for hero in HERO_ORDERS.values()}
    for order in app.all_orders():
        if order["customer_id"] not in hero_customers:
            assert order["email"].endswith("@example.com"), (order["order_id"], order["email"])


# ---------------------------------------------------------------------------------------------------------------------
# Inventory reads
# ---------------------------------------------------------------------------------------------------------------------


def test_inventory_for_one_store(app):
    body = app.ok_json("GET", f"/inventory/{HERO_SKU}?store_id=S-012")
    assert project(body, ("sku", "store_id")) == {"sku": HERO_SKU, "store_id": "S-012"}
    assert isinstance(body["qty"], int) and not isinstance(body["qty"], bool)
    assert 4 <= body["qty"] <= 12


def test_inventory_without_store_lists_all_40_stores(app):
    body = app.ok_json("GET", f"/inventory/{HERO_SKU}")
    assert body["sku"] == HERO_SKU
    store_ids = [row["store_id"] for row in body["stores"]]
    assert len(store_ids) == 40
    assert set(store_ids) == set(STORES)
    stock = {row["store_id"]: row["qty"] for row in body["stores"]}
    assert all(4 <= qty <= 12 for qty in stock.values()), stock
    assert stock["S-012"] == app.stock(HERO_SKU, "S-012")


def test_every_ordered_sku_has_a_stock_row_at_every_store(app):
    skus = sorted({item["sku"] for order in app.all_orders() for item in order["items"]})
    assert HERO_SKU in skus and len(skus) > 1
    for sku in skus:
        body = app.ok_json("GET", f"/inventory/{sku}")
        assert sorted(row["store_id"] for row in body["stores"]) == STORES, sku
        assert all(isinstance(row["qty"], int) and row["qty"] >= 0 for row in body["stores"]), sku


@pytest.mark.parametrize(
    "path",
    [
        "/inventory/NOPE-SKU-1",
        "/inventory/NOPE-SKU-1?store_id=S-001",
        f"/inventory/{HERO_SKU}?store_id=S-999",
        f"/inventory/{HERO_SKU}?store_id=S-000",
    ],
)
def test_unknown_sku_or_store_is_404(app, path):
    response = app.get(path)
    assert response.status_code == 404, response.text
    assert response.json().get("error") == "not_found"


# ---------------------------------------------------------------------------------------------------------------------
# Inventory writes
# ---------------------------------------------------------------------------------------------------------------------


def test_adjust_applies_delta_and_records_one_write(app):
    before = app.stock(HERO_SKU, "S-005")
    body = app.ok_json("POST", "/inventory/adjust", {"sku": HERO_SKU, "store_id": "S-005", "delta": 2, "reason": "x"})
    expected = {"sku": HERO_SKU, "store_id": "S-005", "qty": before + 2, "delta": 2}
    assert project(body, expected) == expected
    assert app.stock(HERO_SKU, "S-005") == before + 2
    state = app.state()
    assert (state["stock_writes"], state["refunds_issued"]) == (1, 0)
    assert [write["kind"] for write in state["last_writes"]] == ["stock_adjust"]


def test_adjust_without_reason_and_down_to_exactly_zero(app):
    before = app.stock(HERO_SKU, "S-006")
    body = app.ok_json("POST", "/inventory/adjust", {"sku": HERO_SKU, "store_id": "S-006", "delta": -before})
    assert body["qty"] == 0
    assert app.stock(HERO_SKU, "S-006") == 0


def test_adjust_below_zero_is_409_and_changes_nothing(app):
    before = app.stock(HERO_SKU, "S-007")
    response = app.post("/inventory/adjust", {"sku": HERO_SKU, "store_id": "S-007", "delta": -(before + 1)})
    assert response.status_code == 409, response.text
    assert response.json().get("error") == "insufficient_stock"
    assert app.stock(HERO_SKU, "S-007") == before
    assert app.state()["stock_writes"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"sku": "NOPE-SKU-1", "store_id": "S-001", "delta": 1},
        {"sku": HERO_SKU, "store_id": "S-999", "delta": 1},
    ],
)
def test_adjust_unknown_sku_or_store_is_404(app, body):
    response = app.post("/inventory/adjust", body)
    assert response.status_code == 404, response.text
    assert response.json().get("error") == "not_found"
    assert app.state()["stock_writes"] == 0


def test_transfer_moves_stock_and_records_one_write(app):
    from_before, to_before = app.stock(HERO_SKU, "S-001"), app.stock(HERO_SKU, "S-002")
    body = app.ok_json("POST", "/inventory/transfer", {**TRANSFER_BODY, "qty": 3})
    expected = {
        "sku": HERO_SKU,
        "from_store": "S-001",
        "to_store": "S-002",
        "qty": 3,
        "from_qty": from_before - 3,
        "to_qty": to_before + 3,
    }
    assert project(body, expected) == expected
    assert app.stock(HERO_SKU, "S-001") == from_before - 3
    assert app.stock(HERO_SKU, "S-002") == to_before + 3
    state = app.state()
    assert state["stock_writes"] == 1
    assert [write["kind"] for write in state["last_writes"]] == ["stock_transfer"]


def test_transfer_of_everything_leaves_zero(app):
    from_before = app.stock(HERO_SKU, "S-010")
    body = app.ok_json(
        "POST", "/inventory/transfer", {"sku": HERO_SKU, "from_store": "S-010", "to_store": "S-011", "qty": from_before}
    )
    assert body["from_qty"] == 0


def test_transfer_insufficient_is_409_and_both_stores_unchanged(app):
    from_before, to_before = app.stock(HERO_SKU, "S-003"), app.stock(HERO_SKU, "S-004")
    body = {"sku": HERO_SKU, "from_store": "S-003", "to_store": "S-004", "qty": from_before + 1}
    response = app.post("/inventory/transfer", body)
    assert response.status_code == 409, response.text
    assert response.json().get("error") == "insufficient_stock"
    assert app.stock(HERO_SKU, "S-003") == from_before
    assert app.stock(HERO_SKU, "S-004") == to_before
    state = app.state()
    assert (state["stock_writes"], state["last_writes"]) == (0, [])


@pytest.mark.parametrize("bad", [{"from_store": "S-999"}, {"to_store": "S-999"}, {"sku": "NOPE-SKU-1"}])
def test_transfer_unknown_sku_or_store_is_404_and_nothing_moves(app, bad):
    stock_before = app.all_stock()
    response = app.post("/inventory/transfer", {**TRANSFER_BODY, **bad})
    assert response.status_code == 404, response.text
    assert response.json().get("error") == "not_found"
    assert app.all_stock() == stock_before
    assert app.state()["stock_writes"] == 0


def test_concurrent_adjusts_are_not_lost(app):
    before = app.stock(HERO_SKU, "S-020")
    statuses = parallel(
        lambda _: app.post("/inventory/adjust", {"sku": HERO_SKU, "store_id": "S-020", "delta": 1}).status_code, 20
    )
    assert statuses == [200] * 20
    assert app.stock(HERO_SKU, "S-020") == before + 20
    assert app.state()["stock_writes"] == 20


def test_concurrent_transfers_never_overdraw(app):
    from_before, to_before = app.stock(HERO_SKU, "S-030"), app.stock(HERO_SKU, "S-031")
    body = {"sku": HERO_SKU, "from_store": "S-030", "to_store": "S-031", "qty": 1}
    statuses = parallel(lambda _: app.post("/inventory/transfer", body).status_code, from_before + 5)
    assert statuses.count(200) == from_before, statuses
    assert statuses.count(409) == 5, statuses
    assert app.stock(HERO_SKU, "S-030") == 0
    assert app.stock(HERO_SKU, "S-031") == to_before + from_before
    assert app.state()["stock_writes"] == from_before


def test_s4_like_run_adjusts_every_store(app):
    before = app.all_stock()
    for n in range(1, 41):
        app.ok_json("POST", "/inventory/adjust", {"sku": HERO_SKU, "store_id": f"S-{n:03d}", "delta": 2})
    assert app.all_stock() == {store: qty + 2 for store, qty in before.items()}
    assert app.state()["stock_writes"] == 40


# ---------------------------------------------------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------------------------------------------------


def test_refund_is_201_with_sequential_ids_from_rf_0001(app):
    first = app.post("/refunds", REFUND_BODY)
    assert first.status_code == 201, first.text
    expected = {"refund_id": "RF-0001", "order_id": "CO-1042", "amount": 120, "status": "issued"}
    assert project(first.json(), expected) == expected
    second = app.post("/refunds", {"order_id": "CO-1077", "amount": 10.5})
    assert second.status_code == 201, second.text
    assert second.json()["refund_id"] == "RF-0002"
    assert second.json()["amount"] == 10.5


def test_refund_row_can_be_read_back(app):
    app.ok_json("POST", "/refunds", {"order_id": "CO-1042", "amount": 12.34, "reason": "damaged"}, expected=201)
    row = app.ok_json("GET", "/refunds/RF-0001")
    assert {"refund_id", "order_id", "amount", "reason", "status", "created_at"} <= set(row)
    expected = {"refund_id": "RF-0001", "order_id": "CO-1042", "reason": "damaged", "status": "issued"}
    assert project(row, expected) == expected
    assert row["amount"] == pytest.approx(12.34)


def test_unknown_refund_is_404(app):
    response = app.get("/refunds/RF-9999")
    assert response.status_code == 404
    assert response.json().get("error") == "not_found"


def test_refund_for_unknown_order_is_404_and_burns_no_id(app):
    response = app.post("/refunds", {"order_id": "CO-9999", "amount": 5})
    assert response.status_code == 404, response.text
    assert response.json().get("error") == "not_found"
    assert app.state()["refunds_issued"] == 0
    assert app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)["refund_id"] == "RF-0001"


def test_there_is_no_refund_total_cap(app):
    # S3b issues one refund on CO-1042 every run; together they exceed the 549.00 order total.
    ids = [app.ok_json("POST", "/refunds", {"order_id": "CO-1042", "amount": 549}, expected=201)["refund_id"]]
    ids += [app.ok_json("POST", "/refunds", {"order_id": "CO-1042", "amount": 549}, expected=201)["refund_id"]]
    ids += [app.ok_json("POST", "/refunds", {"order_id": "CO-1042", "amount": 5000}, expected=201)["refund_id"]]
    assert ids == ["RF-0001", "RF-0002", "RF-0003"]
    assert app.state()["refunds_issued"] == 3


def test_concurrent_refunds_get_unique_sequential_ids(app):
    responses = parallel(lambda _: app.post("/refunds", {"order_id": "CO-1042", "amount": 1}), 20)
    assert [response.status_code for response in responses] == [201] * 20
    ids = sorted(response.json()["refund_id"] for response in responses)
    assert ids == [f"RF-{n:04d}" for n in range(1, 21)]
    assert app.state()["refunds_issued"] == 20


# ---------------------------------------------------------------------------------------------------------------------
# Validation (422)
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/inventory/adjust", {}),
        ("/inventory/adjust", {"sku": HERO_SKU, "store_id": "S-001"}),
        ("/inventory/adjust", {"sku": HERO_SKU, "store_id": "S-001", "delta": "two"}),
        ("/inventory/adjust", {"sku": HERO_SKU, "store_id": "S-001", "delta": 1.5}),
        ("/inventory/adjust", {"store_id": "S-001", "delta": 1}),
        ("/inventory/transfer", {**TRANSFER_BODY, "qty": 0}),
        ("/inventory/transfer", {**TRANSFER_BODY, "qty": -1}),
        ("/inventory/transfer", {**TRANSFER_BODY, "qty": "x"}),
        ("/inventory/transfer", {"sku": HERO_SKU, "from_store": "S-001", "qty": 1}),
        ("/refunds", {"order_id": "CO-1042", "amount": 0}),
        ("/refunds", {"order_id": "CO-1042", "amount": -1}),
        ("/refunds", {"order_id": "CO-1042", "amount": "abc"}),
        ("/refunds", {"order_id": "CO-1042"}),
        ("/refunds", {"amount": 10}),
    ],
)
def test_bad_body_is_422_and_changes_nothing(app, path, body):
    stock_before = app.all_stock()
    response = app.post(path, body)
    assert response.status_code == 422, f"{path} {body}: {response.status_code} {response.text[:300]}"
    _assert_nothing_changed(app, stock_before)


@pytest.mark.parametrize("path", ["/inventory/adjust", "/inventory/transfer", "/refunds"])
def test_non_json_body_is_422(app, path):
    headers = {**bearer(app.token), "content-type": "application/json"}
    response = app.business.post(path, content=b"this is not json", headers=headers)
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------------------------------------------------


def test_admin_state_starts_empty_and_needs_no_auth(app):
    response = app.admin.get("/admin/state")
    assert response.status_code == 200
    body = response.json()
    assert project(body, ("refunds_issued", "stock_writes", "last_writes")) == {
        "refunds_issued": 0,
        "stock_writes": 0,
        "last_writes": [],
    }


def test_admin_state_counts_and_newest_first_with_actor_from_token_sub(app, mint):
    adjuster = mint(sub="mcp-inventory-a")
    mover = mint(sub="mcp-inventory-b")
    refunder = mint(sub="mcp-refunds-c")
    assert app.post("/inventory/adjust", {**ADJUST_BODY, "store_id": "S-008"}, token=adjuster).status_code == 200
    assert app.post("/inventory/transfer", TRANSFER_BODY, token=mover).status_code == 200
    assert app.post("/refunds", REFUND_BODY, token=refunder).status_code == 201
    # Failed writes record nothing.
    assert app.post("/inventory/adjust", {**ADJUST_BODY, "delta": -10_000}).status_code == 409
    assert app.post("/refunds", {"order_id": "CO-9999", "amount": 1}).status_code == 404

    state = app.state()
    assert (state["refunds_issued"], state["stock_writes"]) == (1, 2)
    writes = state["last_writes"]
    assert [write["kind"] for write in writes] == ["refund", "stock_transfer", "stock_adjust"]
    assert [write["actor"] for write in writes] == ["mcp-refunds-c", "mcp-inventory-b", "mcp-inventory-a"]
    for write in writes:
        assert {"ts", "kind", "detail", "actor"} <= set(write)
        assert write["ts"], write
        assert detail_is_json(write), f"detail is not JSON: {write['detail']!r}"


def test_admin_state_last_writes_capped_at_20_newest_first(app, mint):
    # Each write uses its own token sub, so `actor` (the caller's sub) tells the writes apart.
    for n in range(1, 26):
        body = {"sku": HERO_SKU, "store_id": f"S-{n:03d}", "delta": 1}
        assert app.post("/inventory/adjust", body, token=mint(sub=f"writer-{n:02d}")).status_code == 200
    app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)
    state = app.state()
    assert (state["stock_writes"], state["refunds_issued"]) == (25, 1)
    writes = state["last_writes"]
    assert len(writes) == 20
    expected_actors = [SERVICE_SUB] + [f"writer-{n:02d}" for n in range(25, 6, -1)]
    assert [write["actor"] for write in writes] == expected_actors
    assert [write["kind"] for write in writes] == ["refund"] + ["stock_adjust"] * 19


def test_admin_order_returns_raw_row_with_full_pan(app):
    response = app.admin.get("/admin/order/CO-1042")
    assert response.status_code == 200, response.text
    body = response.json()
    row = body["order"] if isinstance(body.get("order"), dict) else body
    expected = {
        "order_id": "CO-1042",
        "customer_id": "C-1001",
        "status": "shipped",
        "total": 549.00,
        "card_brand": "visa",
        "card_number": "4111111111111111",
        "card_exp": "12/29",
    }
    assert project(row, expected) == expected
    raw_items = body["items"] if "items" in body else row["items"]
    items = [project(item, ("sku", "qty", "unit_price")) for item in raw_items]
    assert items == [{"sku": HERO_SKU, "qty": 1, "unit_price": 549.00}]


def test_admin_order_unknown_is_404(app):
    response = app.admin.get("/admin/order/CO-9999")
    assert response.status_code == 404
    assert response.json().get("error") == "not_found"


def test_admin_reset_restores_seed_and_restarts_refund_ids(app):
    stock_before = app.all_stock()
    app.ok_json("POST", "/inventory/adjust", {**ADJUST_BODY, "delta": 5})
    app.ok_json("POST", "/inventory/transfer", {**TRANSFER_BODY, "from_store": "S-002", "to_store": "S-003"})
    app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)
    app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)
    assert app.state()["refunds_issued"] == 2

    started = time.perf_counter()
    response = app.admin.post("/admin/reset")
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "reset", "refunds_issued": 0, "stock_writes": 0}
    assert elapsed < 2.0, f"reset took {elapsed:.2f}s"

    state = app.state()
    assert (state["refunds_issued"], state["stock_writes"], state["last_writes"]) == (0, 0, [])
    assert app.all_stock() == stock_before
    assert app.get("/refunds/RF-0001").status_code == 404
    assert app.get("/refunds/RF-0002").status_code == 404
    assert app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)["refund_id"] == "RF-0001"
    assert app.ok_json("GET", "/orders/CO-1042")["payment"]["card_number"] == "4111111111111111"


def test_admin_reset_twice_in_a_row(app):
    for _ in range(2):
        response = app.admin.post("/admin/reset")
        assert response.status_code == 200
        assert response.json() == {"status": "reset", "refunds_issued": 0, "stock_writes": 0}
    assert len(app.all_orders()) == 200


def test_startup_rebuilds_a_database_left_by_a_previous_run(erp, settings_for, recorder, mint, tmp_path):
    # /tmp is an emptyDir, which survives a container restart: startup must not reuse the old estate.
    db_path = tmp_path / "shared.sqlite3"
    with app_pair(erp, settings_for(db_path), recorder, mint()) as first:
        stock_before = first.all_stock()
        first.ok_json("POST", "/refunds", REFUND_BODY, expected=201)
        first.ok_json("POST", "/inventory/adjust", {**ADJUST_BODY, "delta": 7})
        assert first.state()["refunds_issued"] == 1
    with app_pair(erp, settings_for(db_path), recorder, mint()) as second:
        state = second.state()
        assert (state["refunds_issued"], state["stock_writes"], state["last_writes"]) == (0, 0, [])
        assert second.all_stock() == stock_before
        assert second.get("/refunds/RF-0001").status_code == 404


def test_apps_from_one_settings_share_one_database(app):
    app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)
    assert app.state()["refunds_issued"] == 1
    assert app.admin.post("/admin/reset").status_code == 200
    assert app.get("/refunds/RF-0001").status_code == 404


# ---------------------------------------------------------------------------------------------------------------------
# Logging: writes are logged as erp_write; no PAN, email or token ever reaches the logs
# ---------------------------------------------------------------------------------------------------------------------


def test_logs_carry_writes_but_no_pan_email_or_token(app, capsys):
    capsys.readouterr()  # drop startup output
    orders = app.all_orders()  # S5-like bulk read
    pans = {order["payment"]["card_number"] for order in orders}
    emails = {order["email"] for order in orders}
    assert "4111111111111111" in pans and "alex.rivera@example.com" in emails

    app.ok_json("GET", "/orders/CO-1042")  # S2-like: get_order_details
    app.ok_json("GET", "/orders/CO-1042")  # S3b-like: get_order_details, then issue_refund
    app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)
    app.ok_json("POST", "/inventory/adjust", ADJUST_BODY)  # S4-like
    app.ok_json("POST", "/inventory/transfer", TRANSFER_BODY)
    app.ok_json("GET", "/customers/C-1066/orders")
    assert app.admin.get("/admin/order/CO-1042").status_code == 200  # the cockpit's S2 view
    assert app.admin.get("/admin/state").status_code == 200

    captured = capsys.readouterr()
    text = captured.out + captured.err
    leaked_pans = sorted(pan for pan in pans if pan in text)
    assert not leaked_pans, f"card numbers in logs: {[pan[-4:] for pan in leaked_pans]}"
    assert "4111 1111 1111 1111" not in text
    leaked_emails = sorted(email for email in emails if email in text)
    assert not leaked_emails, f"emails in logs: {leaked_emails[:5]}"
    assert app.token not in text and app.token.split(".")[2] not in text

    events = json_lines(text)
    writes = [event for event in events if event.get("event") == "erp_write"]
    by_kind = {event.get("kind"): event for event in writes}
    assert sorted(event.get("kind") for event in writes) == ["refund", "stock_adjust", "stock_transfer"], writes
    refund = by_kind["refund"]
    assert (refund.get("actor"), refund.get("order_id"), refund.get("amount")) == (SERVICE_SUB, "CO-1042", 120)
    adjust = by_kind["stock_adjust"]
    assert (adjust.get("actor"), adjust.get("sku"), adjust.get("store_id"), adjust.get("delta")) == (
        SERVICE_SUB,
        HERO_SKU,
        "S-001",
        2,
    )
    transfer = by_kind["stock_transfer"]
    assert (transfer.get("actor"), transfer.get("sku")) == (SERVICE_SUB, HERO_SKU)

    reads = [event for event in events if event.get("event") == "http_request"]
    assert any(event.get("path") == "/orders/CO-1042" for event in reads), "reads must log http_request"


def test_admin_state_shows_no_pan_or_email(app):
    app.ok_json("POST", "/refunds", REFUND_BODY, expected=201)
    app.ok_json("POST", "/refunds", {"order_id": "CO-1077", "amount": 499, "reason": "goodwill - recall"}, expected=201)
    text = app.admin.get("/admin/state").text
    for hero in HERO_ORDERS.values():
        assert hero["payment"]["card_number"] not in text
        assert hero["email"] not in text


# ---------------------------------------------------------------------------------------------------------------------
# Origin header on errors too (the recorder checks every response; this test makes sure errors are exercised)
# ---------------------------------------------------------------------------------------------------------------------


def test_error_responses_carry_origin_header(app, mint):
    responses = [
        app.get("/orders/CO-1042", token=None),  # 401
        app.get("/orders/CO-1042", token=mint(svc_role=None)),  # 403
        app.get("/orders/CO-9999"),  # 404
        app.post("/inventory/adjust", {**ADJUST_BODY, "delta": -10_000}),  # 409
        app.post("/refunds", {"order_id": "CO-1042", "amount": 0}),  # 422
        app.admin.get("/admin/order/CO-9999"),  # admin 404
    ]
    assert [response.status_code for response in responses] == [401, 403, 404, 409, 422, 404]
    for response in responses:
        assert response.headers.get("x-copperline-origin"), response.request.url
