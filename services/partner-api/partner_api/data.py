"""Deterministic demo data for Granite Supply. No database and no ERP: everything is generated from fixed seeds, so
every run (and every replica) returns the same purchase orders.

The internal cost sheet and margins are the "shadow" data of S6: plausible-looking, entirely fake, and shaped so
that XC sensitive-data discovery has something to flag (a supplier IBAN, unit costs, margins).
"""

import random

PAGE_SIZE = 25
PO_COUNT = 250  # PO-5001 .. PO-5250: 10 pages, and the published spec allows page 1..100
STATUSES = ("open", "shipped", "closed")
SKUS = ("TNT-ALPINE-2", "SLP-SUMMIT-0", "BOT-RIDGE-MID", "SOK-MERINO-3", "PCK-TRAIL-40", "STV-CAMP-1")


def _purchase_orders() -> list[dict]:
    rng = random.Random(5531)  # fixed seed: same POs on every start
    orders = []
    for number in range(5001, 5001 + PO_COUNT):
        lines = [
            {"sku": rng.choice(SKUS), "qty": rng.randint(4, 120)}
            for _ in range(rng.randint(1, 3))
        ]
        orders.append({
            "po_id": f"PO-{number}",
            "supplier": "granite-supply",
            "status": STATUSES[number % len(STATUSES)],
            "due": f"2026-11-{(number % 28) + 1:02d}",
            "lines": lines,
        })
    return orders


PURCHASE_ORDERS = _purchase_orders()
BY_ID = {po["po_id"]: po for po in PURCHASE_ORDERS}


def page_of(page: int, status: str | None) -> dict:
    rows = [po for po in PURCHASE_ORDERS if status is None or po["status"] == status]
    start = (page - 1) * PAGE_SIZE
    return {"page": page, "page_size": PAGE_SIZE, "total": len(rows), "items": rows[start:start + PAGE_SIZE]}


# ---- shadow data (not in the published OpenAPI) -------------------------------------------------------------------

COST_SHEET = {
    "supplier": "granite-supply",
    "currency": "USD",
    "remit_to": {  # fake bank details; the IBAN uses a valid format and checksum, but no such account exists
        "bank": "Example Mountain Bank",
        "iban": "DE89 3704 0044 0532 0130 00",
        "swift": "EXMBDEFFXXX",
    },
    "unit_costs": [
        {"sku": "TNT-ALPINE-2", "unit_cost": 212.40, "list_price": 549.00},
        {"sku": "SLP-SUMMIT-0", "unit_cost": 188.10, "list_price": 499.00},
        {"sku": "BOT-RIDGE-MID", "unit_cost": 71.25, "list_price": 219.00},
    ],
    "contact": {"name": "Pat Example", "email": "ap@granite-supply.example", "phone": "+1-555-0142"},
}

MARGINS = {
    "currency": "USD",
    "by_category": [
        {"category": "tents", "gross_margin_pct": 61.3},
        {"category": "sleeping", "gross_margin_pct": 62.3},
        {"category": "footwear", "gross_margin_pct": 67.5},
    ],
}
