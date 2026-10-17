"""The ERP database: one SQLite file, one connection, one lock.

FastAPI runs the (sync) endpoints in a thread pool, so every method takes the lock first; that makes each call,
including the read-check-write of a stock move, atomic. The database is rebuilt from seed.sql when a Db is created
(startup) and on reset(). Methods return plain dicts already shaped like the API responses.
"""

import json
import sqlite3
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from erp_mock.settings import SEED_SQL

LAST_WRITES = 20
STOCK_KINDS = ("stock_adjust", "stock_transfer")


class NotFound(Exception):
    """An order, SKU, store or refund that does not exist. str(e) says which."""


class InsufficientStock(Exception):
    """The move would take a stock level below zero. Nothing was changed."""


ORDERS_SQL = """
SELECT o.order_id, o.customer_id, c.name AS customer, c.email, o.status, o.placed_at, o.eta, o.total,
       o.card_brand, o.card_number, o.card_exp
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
"""
ITEMS_SQL = """
SELECT i.order_id, i.sku, p.name, i.qty, i.unit_price
FROM order_items i JOIN products p ON p.sku = i.sku JOIN orders o ON o.order_id = i.order_id
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Weak, so a Db (and its open connection) goes away with the apps that used it.
_shared: "weakref.WeakValueDictionary[Path, Db]" = weakref.WeakValueDictionary()
_shared_lock = threading.Lock()


def shared_db(path: str | Path, seed_path: str | Path = SEED_SQL, *, rebuild: bool = False) -> "Db":
    """The one Db for this file in this process. A new one is built from seed.sql; rebuild=True rebuilds an old one.

    Both apps must use the same Db: a second Db on the same file would rebuild it under the first one's feet."""
    key = Path(path).resolve()
    with _shared_lock:
        db = _shared.get(key)
        if db is None:
            db = Db(key, seed_path)
            _shared[key] = db
        elif rebuild:
            db.reset()
        return db


class Db:
    def __init__(self, path: str | Path, seed_path: str | Path = SEED_SQL) -> None:
        self.path = Path(path)
        self.seed_path = Path(seed_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self.reset()

    # ---- lifecycle ------------------------------------------------------------------------------------------------

    def reset(self) -> None:
        """Throw the database away and rebuild it from seed.sql. Counters start again from zero."""
        seed = self.seed_path.read_text(encoding="utf-8")
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)  # shared across threads, guarded by _lock
            conn.row_factory = sqlite3.Row
            conn.executescript(seed)
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _locked(self) -> Iterator[sqlite3.Connection]:
        """The connection, held exclusively. Writes inside commit together, or roll back on any exception."""
        with self._lock:
            if self._conn is None:
                raise RuntimeError("database is closed")
            with self._conn:
                yield self._conn

    # ---- orders ---------------------------------------------------------------------------------------------------

    def get_order(self, order_id: str) -> dict:
        orders = self._orders("WHERE o.order_id = ?", (order_id,))
        if not orders:
            raise NotFound(f"unknown order {order_id!r}")
        return orders[0]

    def customer_orders(self, customer_id: str) -> list[dict]:
        """Every order of one customer, oldest order_id first. "*" means every customer (S5's bulk export)."""
        if customer_id == "*":
            return self._orders("", ())
        return self._orders("WHERE o.customer_id = ?", (customer_id,))

    def _orders(self, where: str, params: tuple) -> list[dict]:
        with self._locked() as conn:
            rows = conn.execute(f"{ORDERS_SQL} {where} ORDER BY o.order_id", params).fetchall()
            item_rows = conn.execute(f"{ITEMS_SQL} {where} ORDER BY i.rowid", params).fetchall()
        items: dict[str, list[dict]] = {}
        for item in item_rows:
            items.setdefault(item["order_id"], []).append(
                {"sku": item["sku"], "name": item["name"], "qty": item["qty"], "unit_price": item["unit_price"]}
            )
        return [
            {
                "order_id": row["order_id"],
                "customer_id": row["customer_id"],
                "customer": row["customer"],
                "email": row["email"],
                "status": row["status"],
                "placed_at": row["placed_at"],
                "eta": row["eta"],
                "items": items.get(row["order_id"], []),
                "total": row["total"],
                "payment": {"brand": row["card_brand"], "card_number": row["card_number"], "exp": row["card_exp"]},
            }
            for row in rows
        ]

    def raw_order(self, order_id: str) -> dict:
        """The orders row exactly as stored (full PAN), plus its order_items rows under "items"."""
        with self._locked() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            items = conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY rowid", (order_id,)).fetchall()
        if row is None:
            raise NotFound(f"unknown order {order_id!r}")
        return {**dict(row), "items": [dict(item) for item in items]}

    # ---- inventory ------------------------------------------------------------------------------------------------

    def stock_at(self, sku: str, store_id: str) -> dict:
        with self._locked() as conn:
            qty = self._qty(conn, sku, store_id)
        return {"sku": sku, "store_id": store_id, "qty": qty}

    def stock_everywhere(self, sku: str) -> dict:
        with self._locked() as conn:
            self._require_sku(conn, sku)
            rows = conn.execute("SELECT store_id, qty FROM inventory WHERE sku = ? ORDER BY store_id", (sku,))
            stores = [{"store_id": row["store_id"], "qty": row["qty"]} for row in rows]
        return {"sku": sku, "stores": stores}

    def adjust_stock(self, sku: str, store_id: str, delta: int, reason: str | None, actor: str | None) -> dict:
        with self._locked() as conn:
            qty = self._qty(conn, sku, store_id) + delta
            if qty < 0:
                raise InsufficientStock(f"{store_id} has {qty - delta} of {sku}; cannot apply {delta}")
            self._set_qty(conn, sku, store_id, qty)
            detail = {"sku": sku, "store_id": store_id, "delta": delta, "qty": qty, "reason": reason}
            self._record(conn, "stock_adjust", detail, actor)
        return {"sku": sku, "store_id": store_id, "qty": qty, "delta": delta}

    def transfer_stock(self, sku: str, from_store: str, to_store: str, qty: int, actor: str | None) -> dict:
        with self._locked() as conn:
            from_qty = self._qty(conn, sku, from_store) - qty
            to_qty = self._qty(conn, sku, to_store) + qty
            if from_qty < 0:
                raise InsufficientStock(f"{from_store} has {from_qty + qty} of {sku}; cannot move {qty}")
            self._set_qty(conn, sku, from_store, from_qty)
            self._set_qty(conn, sku, to_store, to_qty)
            result = {"sku": sku, "from_store": from_store, "to_store": to_store, "qty": qty,
                      "from_qty": from_qty, "to_qty": to_qty}
            self._record(conn, "stock_transfer", result, actor)
        return result

    def _qty(self, conn: sqlite3.Connection, sku: str, store_id: str) -> int:
        row = conn.execute("SELECT qty FROM inventory WHERE sku = ? AND store_id = ?", (sku, store_id)).fetchone()
        if row is not None:
            return row["qty"]
        self._require_sku(conn, sku)
        raise NotFound(f"unknown store {store_id!r}")  # every SKU has a row at every store

    @staticmethod
    def _require_sku(conn: sqlite3.Connection, sku: str) -> None:
        if conn.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone() is None:
            raise NotFound(f"unknown sku {sku!r}")

    @staticmethod
    def _set_qty(conn: sqlite3.Connection, sku: str, store_id: str, qty: int) -> None:
        conn.execute("UPDATE inventory SET qty = ? WHERE sku = ? AND store_id = ?", (qty, sku, store_id))

    # ---- refunds --------------------------------------------------------------------------------------------------

    def issue_refund(self, order_id: str, amount: float, reason: str | None, actor: str | None) -> dict:
        """Always issues: there is no cap on the refund total (S3b refunds CO-1042 on every run)."""
        with self._locked() as conn:
            if conn.execute("SELECT 1 FROM orders WHERE order_id = ?", (order_id,)).fetchone() is None:
                raise NotFound(f"unknown order {order_id!r}")
            issued = conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]
            refund_id = f"RF-{issued + 1:04d}"  # sequential since the last reset
            conn.execute(
                "INSERT INTO refunds (refund_id, order_id, amount, reason, status, created_at)"
                " VALUES (?, ?, ?, ?, 'issued', ?)",
                (refund_id, order_id, amount, reason, now_iso()),
            )
            detail = {"refund_id": refund_id, "order_id": order_id, "amount": amount, "reason": reason}
            self._record(conn, "refund", detail, actor)
        return {"refund_id": refund_id, "order_id": order_id, "amount": amount, "status": "issued"}

    def get_refund(self, refund_id: str) -> dict:
        with self._locked() as conn:
            row = conn.execute("SELECT * FROM refunds WHERE refund_id = ?", (refund_id,)).fetchone()
        if row is None:
            raise NotFound(f"unknown refund {refund_id!r}")
        return dict(row)

    # ---- the write log (the cockpit's Estate panel) ----------------------------------------------------------------

    @staticmethod
    def _record(conn: sqlite3.Connection, kind: str, detail: dict, actor: str | None) -> None:
        """One row per successful write, in the same transaction as the write itself."""
        conn.execute(
            "INSERT INTO writes (ts, kind, detail, actor) VALUES (?, ?, ?, ?)",
            (now_iso(), kind, json.dumps(detail, sort_keys=True), actor),
        )

    def state(self) -> dict:
        """Counts since the last reset, plus the newest writes first."""
        with self._locked() as conn:
            counts = dict(conn.execute("SELECT kind, COUNT(*) FROM writes GROUP BY kind").fetchall())
            rows = conn.execute("SELECT ts, kind, detail, actor FROM writes ORDER BY id DESC LIMIT ?", (LAST_WRITES,))
            last = [
                {"ts": row["ts"], "kind": row["kind"], "detail": json.loads(row["detail"]), "actor": row["actor"]}
                for row in rows
            ]
        return {
            "refunds_issued": counts.get("refund", 0),
            "stock_writes": sum(counts.get(kind, 0) for kind in STOCK_KINDS),
            "last_writes": last,
        }
