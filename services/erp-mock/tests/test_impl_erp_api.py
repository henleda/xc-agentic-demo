"""erp-mock business + admin API: hero rows, shapes, stock rules, refunds, the estate counters and scenario shapes.

Fixtures (conftest.py): `business` and `admin` are TestClients on ONE fresh database; `auth` is a valid svc-mcp token.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

ALPINE = "TNT-ALPINE-2"
STORES = [f"S-{n:03d}" for n in range(1, 41)]
PANS = ("4111111111111111", "5555555555554444", "378282246310005")


def ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def stock(business, auth, sku: str, store_id: str) -> int:
    return ok(business.get(f"/inventory/{sku}?store_id={store_id}", headers=auth))["qty"]


def adjust(business, auth, store_id: str, delta: int, sku: str = ALPINE):
    return business.post("/inventory/adjust", headers=auth, json={"sku": sku, "store_id": store_id, "delta": delta})


def transfer(business, auth, from_store: str, to_store: str, qty: int, sku: str = ALPINE):
    body = {"sku": sku, "from_store": from_store, "to_store": to_store, "qty": qty}
    return business.post("/inventory/transfer", headers=auth, json=body)


def refund(business, auth, order_id: str = "CO-1042", amount=120, reason: str | None = "damaged"):
    return business.post("/refunds", headers=auth, json={"order_id": order_id, "amount": amount, "reason": reason})


# ---- hero rows ----------------------------------------------------------------------------------------------------


def test_hero_order_co_1042_exact(business, auth):
    assert ok(business.get("/orders/CO-1042", headers=auth)) == {
        "order_id": "CO-1042",
        "customer_id": "C-1001",
        "customer": "Alex Rivera",
        "email": "alex.rivera@example.com",
        "status": "shipped",
        "placed_at": "2026-10-11T16:42:00Z",
        "eta": "2026-10-20",
        "items": [{"sku": ALPINE, "name": "Alpine 2 Tent", "qty": 1, "unit_price": 549.0}],
        "total": 549.0,
        "payment": {"brand": "visa", "card_number": "4111111111111111", "exp": "12/29"},  # contiguous digits
    }


def test_hero_order_co_1077_exact(business, auth):
    order = ok(business.get("/orders/CO-1077", headers=auth))
    assert (order["customer_id"], order["customer"], order["email"]) == ("C-1066", "Mallory", "mallory.x@example.net")
    assert (order["status"], order["total"]) == ("delivered", 499.0)
    assert order["payment"] == {"brand": "mastercard", "card_number": "5555555555554444", "exp": "03/28"}
    assert sum(item["qty"] * item["unit_price"] for item in order["items"]) == 499.0


def test_hero_order_co_1100_exact(business, auth):
    order = ok(business.get("/orders/CO-1100", headers=auth))
    assert (order["customer_id"], order["customer"], order["email"]) == ("C-1100", "Pat Gomez", "pat.gomez@example.org")
    assert order["status"] == "processing"
    assert order["payment"] == {"brand": "amex", "card_number": "378282246310005", "exp": "07/27"}
    assert order["total"] == sum(item["qty"] * item["unit_price"] for item in order["items"])


@pytest.mark.parametrize(
    ("customer_id", "order_id"), [("C-1001", "CO-1042"), ("C-1066", "CO-1077"), ("C-1100", "CO-1100")]
)
def test_each_hero_customer_has_exactly_their_hero_order(business, auth, customer_id, order_id):
    body = ok(business.get(f"/customers/{customer_id}/orders", headers=auth))
    assert body["customer_id"] == customer_id
    assert [o["order_id"] for o in body["orders"]] == [order_id]
    assert body["orders"][0] == ok(business.get(f"/orders/{order_id}", headers=auth))


def test_seed_shape(business, auth, db):
    orders = ok(business.get("/customers/*/orders", headers=auth))["orders"]
    assert [o["order_id"] for o in orders] == [f"CO-{n}" for n in range(1000, 1200)]
    assert all(o["total"] == round(sum(i["qty"] * i["unit_price"] for i in o["items"]), 2) for o in orders)
    others = [o for o in orders if o["customer_id"] not in ("C-1001", "C-1066", "C-1100")]
    assert all(o["email"].endswith("@example.com") for o in others)
    assert not {o["payment"]["card_number"] for o in others} & set(PANS)  # hero PANs stay unique to hero orders

    alpine = ok(business.get(f"/inventory/{ALPINE}", headers=auth))
    assert [row["store_id"] for row in alpine["stores"]] == STORES
    assert all(4 <= row["qty"] <= 12 for row in alpine["stores"])
    with db._locked() as conn:  # every SKU x store has a row
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 60
        assert conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 60 * 40
        assert conn.execute("SELECT name, price FROM products WHERE sku = ?", (ALPINE,)).fetchone()[:] == (
            "Alpine 2 Tent", 549.0)


# ---- reads --------------------------------------------------------------------------------------------------------


def test_unknown_customer_has_no_orders(business, auth):
    assert ok(business.get("/customers/C-9999/orders", headers=auth)) == {"customer_id": "C-9999", "orders": []}


@pytest.mark.parametrize(
    "path",
    ["/orders/CO-9999", "/inventory/NOPE-1", "/inventory/NOPE-1?store_id=S-001", f"/inventory/{ALPINE}?store_id=S-041",
     "/refunds/RF-0001", "/no/such/route"],
)
def test_missing_things_are_404_not_found(business, auth, path):
    response = business.get(path, headers=auth)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found" and response.json()["detail"]


def test_stock_one_store(business, auth):
    assert ok(business.get(f"/inventory/{ALPINE}?store_id=S-012", headers=auth)).keys() == {"sku", "store_id", "qty"}


def test_empty_store_id_means_all_stores(business, auth):
    assert len(ok(business.get(f"/inventory/{ALPINE}?store_id=", headers=auth))["stores"]) == 40


def test_wrong_method_is_405_json(business, auth):
    response = business.delete("/orders/CO-1042", headers=auth)
    assert response.status_code == 405 and response.json()["error"] == "method_not_allowed"


def test_business_app_has_no_admin_routes_or_docs(business, auth):
    for path in ("/admin/state", "/docs", "/openapi.json"):
        assert business.get(path, headers=auth).status_code == 404
    assert business.post("/admin/reset", headers=auth).status_code == 404


# ---- stock writes -------------------------------------------------------------------------------------------------


def test_adjust_applies_delta_and_returns_new_qty(business, auth):
    before = stock(business, auth, ALPINE, "S-007")
    assert ok(adjust(business, auth, "S-007", 3)) == {"sku": ALPINE, "store_id": "S-007", "qty": before + 3, "delta": 3}
    assert ok(adjust(business, auth, "S-007", -1))["qty"] == before + 2
    assert stock(business, auth, ALPINE, "S-007") == before + 2


def test_adjust_to_exactly_zero_is_allowed(business, auth):
    before = stock(business, auth, ALPINE, "S-003")
    assert ok(adjust(business, auth, "S-003", -before))["qty"] == 0


def test_adjust_below_zero_is_409_and_changes_nothing(business, admin, auth):
    before = stock(business, auth, ALPINE, "S-003")
    response = adjust(business, auth, "S-003", -(before + 1))
    assert response.status_code == 409 and response.json()["error"] == "insufficient_stock"
    assert stock(business, auth, ALPINE, "S-003") == before
    assert ok(admin.get("/admin/state"))["stock_writes"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"sku": ALPINE, "store_id": "S-001"},
        {"sku": ALPINE, "store_id": "S-001", "delta": "2"},
        {"sku": ALPINE, "store_id": "S-001", "delta": 1.5},
        {"sku": ALPINE, "store_id": "S-001", "delta": True},
        {"store_id": "S-001", "delta": 1},
    ],
)
def test_adjust_bad_body_is_422(business, auth, body):
    assert business.post("/inventory/adjust", headers=auth, json=body).status_code == 422


def test_adjust_unknown_sku_or_store_is_404(business, auth):
    assert adjust(business, auth, "S-001", 1, sku="NOPE-1").status_code == 404
    assert adjust(business, auth, "S-099", 1).status_code == 404


def test_transfer_moves_stock_atomically(business, auth):
    a, b = stock(business, auth, ALPINE, "S-001"), stock(business, auth, ALPINE, "S-002")
    assert ok(transfer(business, auth, "S-001", "S-002", 2)) == {
        "sku": ALPINE, "from_store": "S-001", "to_store": "S-002", "qty": 2, "from_qty": a - 2, "to_qty": b + 2,
    }
    assert (stock(business, auth, ALPINE, "S-001"), stock(business, auth, ALPINE, "S-002")) == (a - 2, b + 2)


def test_transfer_more_than_available_is_409_and_changes_nothing(business, admin, auth):
    a, b = stock(business, auth, ALPINE, "S-001"), stock(business, auth, ALPINE, "S-002")
    response = transfer(business, auth, "S-001", "S-002", a + 1)
    assert response.status_code == 409 and response.json()["error"] == "insufficient_stock"
    assert (stock(business, auth, ALPINE, "S-001"), stock(business, auth, ALPINE, "S-002")) == (a, b)
    assert ok(admin.get("/admin/state"))["stock_writes"] == 0


def test_transfer_to_unknown_store_is_404_and_source_unchanged(business, auth):
    a = stock(business, auth, ALPINE, "S-001")
    assert transfer(business, auth, "S-001", "S-404", 1).status_code == 404
    assert stock(business, auth, ALPINE, "S-001") == a


@pytest.mark.parametrize("qty", [0, -1, "1", 1.0, True])
def test_transfer_qty_must_be_a_positive_int(business, auth, qty):
    assert transfer(business, auth, "S-001", "S-002", qty).status_code == 422


def test_transfer_to_same_store_is_422(business, auth):
    assert transfer(business, auth, "S-001", "S-001", 1).status_code == 422


def test_concurrent_writes_never_lose_an_update(business, admin, auth):
    """FastAPI runs sync endpoints in a thread pool: 40 parallel +1 adjusts on one row must all land."""
    before = stock(business, auth, ALPINE, "S-020")
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: adjust(business, auth, "S-020", 1).status_code, range(40)))
    assert statuses == [200] * 40
    assert stock(business, auth, ALPINE, "S-020") == before + 40
    assert ok(admin.get("/admin/state"))["stock_writes"] == 40


def test_concurrent_drain_never_goes_negative(business, auth):
    """Parallel -1s against a small stock: exactly `before` succeed, the rest are 409, the row ends at 0."""
    before = stock(business, auth, ALPINE, "S-021")
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: adjust(business, auth, "S-021", -1).status_code, range(before + 6)))
    assert statuses.count(200) == before and statuses.count(409) == 6
    assert stock(business, auth, ALPINE, "S-021") == 0


# ---- refunds ------------------------------------------------------------------------------------------------------


def test_refund_is_201_with_sequential_ids(business, auth):
    assert ok(refund(business, auth), 201) == {
        "refund_id": "RF-0001", "order_id": "CO-1042", "amount": 120.0, "status": "issued"
    }
    assert ok(refund(business, auth, "CO-1077", 12.5, None), 201)["refund_id"] == "RF-0002"
    row = ok(business.get("/refunds/RF-0002", headers=auth))
    assert {k: row[k] for k in ("refund_id", "order_id", "amount", "reason", "status")} == {
        "refund_id": "RF-0002", "order_id": "CO-1077", "amount": 12.5, "reason": None, "status": "issued"
    }
    assert row["created_at"].endswith("Z")


def test_refund_reason_is_optional(business, auth):
    response = business.post("/refunds", headers=auth, json={"order_id": "CO-1042", "amount": 5})
    assert response.status_code == 201


def test_refund_unknown_order_is_404_and_uses_no_id(business, auth):
    assert refund(business, auth, "CO-9999").status_code == 404
    assert ok(refund(business, auth), 201)["refund_id"] == "RF-0001"


@pytest.mark.parametrize("amount", [0, -5, "120", True, None])
def test_refund_amount_must_be_a_positive_number(business, auth, amount):
    assert refund(business, auth, amount=amount).status_code == 422


def test_s3b_three_refunds_on_co_1042_all_succeed_no_cap(business, admin, auth):
    """S3b refunds $120 on the 549.00 order once per run; three runs in a row must all get 201 (no cap)."""
    ids = [ok(refund(business, auth, "CO-1042", 120, "damaged"), 201)["refund_id"] for _ in range(3)]
    assert ids == ["RF-0001", "RF-0002", "RF-0003"]
    assert ok(admin.get("/admin/state"))["refunds_issued"] == 3
    big = ok(refund(business, auth, "CO-1042", 10_000), 201)  # even more than the order total
    assert big["refund_id"] == "RF-0004"


# ---- admin: state, raw order, reset -------------------------------------------------------------------------------


def test_admin_state_starts_empty(admin):
    assert ok(admin.get("/admin/state")) == {"refunds_issued": 0, "stock_writes": 0, "last_writes": []}


def test_admin_state_deltas_after_adjust_transfer_refund(business, admin, auth):
    ok(adjust(business, auth, "S-001", 2))
    assert ok(admin.get("/admin/state"))["stock_writes"] == 1
    ok(transfer(business, auth, "S-001", "S-002", 1))
    assert ok(admin.get("/admin/state"))["stock_writes"] == 2
    ok(refund(business, auth), 201)
    state = ok(admin.get("/admin/state"))
    assert (state["refunds_issued"], state["stock_writes"]) == (1, 2)
    assert [w["kind"] for w in state["last_writes"]] == ["refund", "stock_transfer", "stock_adjust"]  # newest first
    assert all(w.keys() == {"ts", "kind", "detail", "actor"} for w in state["last_writes"])
    assert all(w["actor"] == "mcp-servers" for w in state["last_writes"])  # the token's sub
    assert state["last_writes"][0]["detail"] == {
        "refund_id": "RF-0001", "order_id": "CO-1042", "amount": 120.0, "reason": "damaged"
    }
    assert state["last_writes"][2]["detail"]["delta"] == 2


def test_failed_writes_are_not_counted(business, admin, auth):
    adjust(business, auth, "S-001", -999)
    transfer(business, auth, "S-001", "S-002", 999)
    refund(business, auth, "CO-9999")
    business.post("/refunds", json={"order_id": "CO-1042", "amount": 1})  # no token
    assert ok(admin.get("/admin/state")) == {"refunds_issued": 0, "stock_writes": 0, "last_writes": []}


def test_last_writes_keeps_the_newest_20(business, admin, auth):
    for n in range(25):
        ok(adjust(business, auth, "S-005", 1 if n % 2 == 0 else -1))
    state = ok(admin.get("/admin/state"))
    assert state["stock_writes"] == 25
    assert len(state["last_writes"]) == 20
    assert [w["detail"]["delta"] for w in state["last_writes"]][:2] == [1, -1]  # write #25 (+1), then #24 (-1)


def test_admin_order_is_the_raw_row_with_items(admin):
    body = ok(admin.get("/admin/order/CO-1042"))
    assert body == {
        "order_id": "CO-1042", "customer_id": "C-1001", "status": "shipped", "placed_at": "2026-10-11T16:42:00Z",
        "eta": "2026-10-20", "total": 549.0, "card_brand": "visa", "card_number": "4111111111111111",
        "card_exp": "12/29", "items": [{"order_id": "CO-1042", "sku": ALPINE, "qty": 1, "unit_price": 549.0}],
    }
    assert admin.get("/admin/order/CO-9999").status_code == 404


def test_admin_needs_no_token_and_ignores_one(admin, auth):
    assert admin.get("/admin/state").status_code == 200
    assert admin.get("/admin/state", headers={"Authorization": "Bearer junk"}).status_code == 200


def test_admin_healthz_and_origin(admin):
    response = admin.get("/healthz")
    assert response.json() == {"status": "ok", "service": "erp-mock-admin"}
    assert admin.get("/admin/state").headers["x-copperline-origin"] == "erp-mock-admin"


def test_admin_app_does_not_serve_business_routes(admin):
    assert admin.get("/orders/CO-1042").status_code == 404


def test_reset_restores_counts_stock_and_refund_ids(business, admin, auth):
    seed_stock = ok(business.get(f"/inventory/{ALPINE}", headers=auth))
    ok(adjust(business, auth, "S-001", 5))
    ok(transfer(business, auth, "S-002", "S-003", 1))
    ok(refund(business, auth), 201)
    ok(refund(business, auth), 201)

    started = time.monotonic()
    body = ok(admin.post("/admin/reset"))
    assert time.monotonic() - started < 2.0
    assert body == {"status": "reset", "refunds_issued": 0, "stock_writes": 0}
    assert ok(admin.get("/admin/state")) == {"refunds_issued": 0, "stock_writes": 0, "last_writes": []}
    assert ok(business.get(f"/inventory/{ALPINE}", headers=auth)) == seed_stock
    assert business.get("/refunds/RF-0001", headers=auth).status_code == 404
    assert ok(refund(business, auth), 201)["refund_id"] == "RF-0001"  # sequential since reset


def test_reset_under_concurrent_traffic_is_safe(business, admin, auth):
    def write(n: int) -> int:
        return adjust(business, auth, STORES[n % 40], 1).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        writes = pool.map(write, range(60))
        resets = [pool.submit(admin.post, "/admin/reset") for _ in range(3)]
        assert set(writes) == {200}
        assert all(r.result().status_code == 200 for r in resets)
    assert ok(admin.get("/admin/state"))["stock_writes"] <= 60


# ---- scenario shapes ----------------------------------------------------------------------------------------------


def test_s4_forty_adjusts_of_plus_two_all_succeed(business, admin, auth):
    """Without XC's rate limit (local mode) all 40 S4 adjusts land; erp-mock itself never throttles."""
    before = ok(business.get(f"/inventory/{ALPINE}", headers=auth))["stores"]
    for n in range(1, 41):
        body = {"sku": ALPINE, "store_id": f"S-{n:03d}", "delta": 2, "reason": "rebalance"}
        assert ok(business.post("/inventory/adjust", headers=auth, json=body))["delta"] == 2
    after = ok(business.get(f"/inventory/{ALPINE}", headers=auth))["stores"]
    assert [row["qty"] for row in after] == [row["qty"] + 2 for row in before]
    state = ok(admin.get("/admin/state"))
    assert (state["stock_writes"], state["refunds_issued"]) == (40, 0)
    assert all(w["detail"]["reason"] == "rebalance" for w in state["last_writes"])


def test_s5_bulk_export_returns_every_order_with_pans(business, auth):
    orders = ok(business.get("/customers/*/orders", headers=auth))["orders"]
    assert len(orders) == 200
    assert {"4111111111111111", "5555555555554444", "378282246310005"} <= {o["payment"]["card_number"] for o in orders}


# ---- logging ------------------------------------------------------------------------------------------------------


def test_writes_are_logged_as_erp_write_without_pan_or_email(business, auth, read_logs):
    read_logs()
    ok(adjust(business, auth, "S-001", 2))
    ok(transfer(business, auth, "S-001", "S-002", 1))
    ok(refund(business, auth, "CO-1042", 120), 201)
    business.get("/orders/CO-1042", headers=auth)
    business.get("/customers/*/orders", headers=auth)
    records, text = read_logs()

    writes = [r for r in records if r["event"] == "erp_write"]
    assert [w["kind"] for w in writes] == ["stock_adjust", "stock_transfer", "refund"]
    assert all(w["actor"] == "mcp-servers" and w["request_id"] for w in writes)
    assert (writes[0]["sku"], writes[0]["store_id"], writes[0]["delta"]) == (ALPINE, "S-001", 2)
    assert (writes[2]["order_id"], writes[2]["amount"]) == ("CO-1042", 120.0)
    reads = [r for r in records if r["event"] == "http_request" and r["method"] == "GET"]
    assert {r["path"] for r in reads} == {"/orders/CO-1042", "/customers/*/orders"}

    for secret in (*PANS, "4111 1111 1111 1111", "@example.com", "@example.net", "@example.org"):
        assert secret not in text, f"{secret!r} leaked into the logs"
    for line in text.splitlines():
        json.loads(line)  # every stdout line is JSON


def test_x_request_id_is_echoed(business, auth):
    response = business.get("/orders/CO-1042", headers={**auth, "x-request-id": "S2-abc123"})
    assert response.headers["x-request-id"] == "S2-abc123"
    assert response.headers["x-copperline-origin"] == "erp-mock"


# Review finding: out-of-range numbers used to reach SQLite and come back as 500. They are a 422 now.
@pytest.mark.parametrize(
    "path, body",
    [
        ("/inventory/adjust", {"sku": ALPINE, "store_id": "S-001", "delta": 10**30}),
        ("/inventory/adjust", {"sku": ALPINE, "store_id": "S-001", "delta": -(10**30)}),
        ("/inventory/transfer", {"sku": ALPINE, "from_store": "S-001", "to_store": "S-002", "qty": 10**30}),
        ("/refunds", {"order_id": "CO-1042", "amount": 1e308}),
    ],
)
def test_out_of_range_numbers_are_422_not_500(business, auth, path, body):
    assert business.post(path, headers=auth, json=body).status_code == 422


def test_nan_and_infinity_amounts_are_422(business, auth):
    for raw in ('{"order_id":"CO-1042","amount":NaN}', '{"order_id":"CO-1042","amount":Infinity}'):
        resp = business.post("/refunds", headers={**auth, "content-type": "application/json"}, content=raw)
        assert resp.status_code == 422, raw
