# erp-mock: Copperline's system of record (EC2 `copperline-core`, port 8081)

This plays the ERP that Copperline "already owned". In the demo it lives outside vK8s, on the customer's EC2 host, and is reachable only through `erp-lb`, which is advertised on the CE only. Keep it plain: FastAPI, the standard-library `sqlite3`, and no ORM.

## Processes and ports

The container runs **one** Python process that serves two FastAPI apps:

| App | Port | Reachable from |
|---|---|---|
| Business API | **8081**, all interfaces | XC `erp-lb` (`op-erp`), the local-gateway in compose |
| Admin API | **8181**, all interfaces *inside the container* | Only the host's loopback: compose publishes it as `127.0.0.1:8181:8181`. The cockpit and `reset_demo.sh` use it. XC never routes here |

The two apps share one SQLite database. Start it with `python -m erp_mock`, which runs both under uvicorn (two `uvicorn.Server`s in one event loop; SIGTERM stops both).

| Env | Default | Meaning |
|---|---|---|
| `ERP_DB_PATH` | `/tmp/erp.sqlite3` | SQLite file (see Data) |
| `ISSUER` | `https://idp.copperline.demo` | Required `iss` of every business-API token |
| `JWKS_PATH` | unset | JWKS file with the IdP's public keys. Wins over `IDP_BASE_URL` |
| `IDP_BASE_URL` | unset | Used only when `JWKS_PATH` is unset. With neither set, erp-mock refuses to start (exit 2) |
| `ERP_PORT` / `ERP_ADMIN_PORT` | `8081` / `8181` | Listen ports, both on `0.0.0.0` |

## Data

- **Database.** SQLite at `${ERP_DB_PATH}`, default `/tmp/erp.sqlite3`. The image has a read-only root, and `/tmp` is an `emptyDir`.
- **Rebuild.** At startup and on every `POST /admin/reset`, the database is rebuilt from `services/erp-mock/erp_mock/seed.sql`, which is baked into the image. "At startup" is unconditional: an existing file at `ERP_DB_PATH` (for example, `/tmp` surviving a container restart) is thrown away, so a restart never carries refunds or stock changes over from an earlier run.
- **Concurrency.** One connection behind one lock. Each request, including the read-check-write of a stock move or a refund ID, is atomic, and each write commits together with its `writes` row.
- **Where `seed.sql` comes from.** `scripts/gen_seed_data.py` generates it. It is committed, so changes show in diffs. `make check` fails if it is stale (`gen_seed_data.py --check`).

### Hero rows (the scenarios depend on these exact values)

| Thing | Value |
|---|---|
| Alex Rivera | customer `C-1001`, email `alex.rivera@example.com` |
| Mallory | customer `C-1066`, email `mallory.x@example.net` |
| Pat Gomez | customer `C-1100`, email `pat.gomez@example.org` |
| Order `CO-1042` | customer `C-1001`; status `shipped`; one item `TNT-ALPINE-2` × 1 at 549.00, total 549.00; payment `{brand: "visa", card_number: "4111111111111111", exp: "12/29"}`. The PAN is stored as **contiguous digits** |
| Order `CO-1077` | customer `C-1066`; status `delivered`; one item `SLP-SUMMIT-0` ("Summit Zero Down Sleeping Bag") × 1 at 499.00, total 499.00 (the S1 injection's refund target); payment `{brand: "mastercard", card_number: "5555555555554444", exp: "03/28"}` |
| Order `CO-1100` | customer `C-1100`; status `processing`; items `BOT-RIDGE-MID` × 1 at 219.00 and `SOK-MERINO-3` × 2 at 54.00, total 327.00; payment `{brand: "amex", card_number: "378282246310005", exp: "07/27"}` |
| Hero dates | `CO-1042` placed `2026-10-11T16:42:00Z`, eta `2026-10-20`; `CO-1077` placed `2026-09-02T19:15:00Z`, eta `2026-09-08`; `CO-1100` placed `2026-10-14T09:30:00Z`, eta `2026-10-22` |
| Stores | 40 stores, `S-001` … `S-040`. Jordan Lee's store `S-012` is one of them |
| SKUs | 60 SKUs; `TNT-ALPINE-2` ("Alpine 2 Tent", 549.00) is one of them. Every SKU has a stock row at every store |
| `TNT-ALPINE-2` stock | `S-001`…`S-040` start at 4…12 units (deterministic) |

Customers and orders are generated deterministically from a fixed random seed: 200 orders in total, `CO-1000` … `CO-1199`. Every PAN is a published **test** card number and passes the Luhn check. Customers other than the three above have emails at `example.com`.

The rest of the generated data:

- **Customers.** There are 150, `C-1000` … `C-1149`, with unique emails.
- **Hero orders.** Generated orders never go to the three hero customers, so each hero customer has exactly one order: their hero order. That way S0's "my order" and the object-level check in mcp-orders are never ambiguous.
- **Hero PANs.** The three hero PANs appear only on the hero orders.
- **Generated orders.**
  - Status and `eta` follow from each order's age on a fixed "today" of 2026-10-15, so nothing depends on the clock.
  - Statuses are `processing`, `shipped`, `delivered` or `cancelled`. A cancelled order has `eta: null`.
  - Every order has at least one item, and `total` is the sum of `qty × unit_price` over its items.
  - Money is stored as SQLite `REAL`, and every seeded price is a whole-dollar amount.
- **Payment details.** `docs/03-services.md` shows "—" as the card on file for Mallory and Pat. The table above is the authoritative one: those orders carry the Mastercard and Amex test cards.

### Tables

| Table | Columns |
|---|---|
| `stores` | `store_id` (PK), `name`, `city` |
| `products` | `sku` (PK), `name`, `category`, `price` |
| `customers` | `customer_id` (PK), `name`, `email` |
| `orders` | `order_id` (PK), `customer_id`, `status`, `placed_at`, `eta`, `total`, `card_brand`, `card_number`, `card_exp` |
| `order_items` | `order_id`, `sku`, `qty`, `unit_price` |
| `inventory` | (`sku`, `store_id`) PK, `qty` |
| `refunds` | `refund_id` (PK, `RF-0001`…, sequential since reset), `order_id`, `amount`, `reason`, `status` (`issued`), `created_at` |
| `writes` | `id` (autoincrement), `ts`, `kind` (`refund` / `stock_adjust` / `stock_transfer`), `detail` (JSON text), `actor` (the caller's token `sub`) |

## Auth (business API only)

- **Requirement.** Every business route needs `Authorization: Bearer <jwt>`, verified with `copperline_common.jwt.JwksVerifier`:
  - audience `copperline-erp`;
  - issuer from env `ISSUER` (default `https://idp.copperline.demo`);
  - claim `svc_role == "tool-server"`, an exact, case-sensitive match.
  - An `aud` list that contains `copperline-erp` is accepted.
- **Keys.** They come from `${JWKS_PATH}` (a file; on EC2 the host cannot resolve vK8s names). If that is unset, they come from `${IDP_BASE_URL}/.well-known/jwks.json`.
- **Failures.**
  - A missing or invalid token gets **401** `{"error":"unauthorized","detail":…}`.
    - "Invalid" covers a non-Bearer scheme, a token that isn't a JWT, a bad signature or unknown `kid`, the wrong `aud` or `iss`, an expired token, no `exp`, and any `alg` other than RS256.
  - A valid token without `svc_role=tool-server` gets **403** `{"error":"forbidden","detail":…}`. That includes agent and user tokens minted for `copperline-erp`.
  - If erp-mock cannot load its keys (the JWKS file is missing or unreadable, or the IdP is down), it returns **503** `{"error":"unavailable","detail":…}` instead of blaming the caller with a 401.
- **Order.** Auth runs as middleware before routing and body parsing. A caller without a good token gets 401/403 even on an unknown path, a missing resource or a malformed body; it never gets the 404 or 422 that would describe the API.
- **Why it matters.** XC `erp-lb` enforces the same rule. This check is defense in depth, and it is what makes the local stack honest.
- **Exempt.** `GET /healthz` needs no auth.

## Business API (port 8081)

All responses are JSON, errors included, and carry `x-request-id` and `x-copperline-origin: erp-mock`.

- **404.** A missing resource gets **404** `{"error":"not_found","detail":…}`, and so does an unknown path. A wrong method gets **405** `{"error":"method_not_allowed","detail":…}`.
- **422.** A bad body gets **422** with FastAPI's default validation body `{"detail":[…]}`. That covers a missing field, a body that isn't JSON, and any of the following.
  - Types are **strict**. JSON `true`, `"2"` and `1.5` are not valid as `delta` or `qty`, and `true` and `"120"` are not valid as `amount`.
  - A transfer with `from_store == to_store` is a 422.
- **Failed calls.** A 4xx write changes nothing and records no `writes` row.

| Route | Behavior |
|---|---|
| `GET /orders/{order_id}` | `{order_id, customer_id, customer, email, status, placed_at, eta, items:[{sku,name,qty,unit_price}], total, payment:{brand, card_number, exp}}`. The **full PAN**: mcp-orders decides what to mask, and Data Guard masks at tools-lb (S2) |
| `GET /customers/{customer_id}/orders` | `{customer_id, orders:[<same shape as above>]}`, sorted by `order_id`. `customer_id = *` (or `%2A`) returns all 200 orders (S5's bulk export). An unknown customer returns `orders: []` |
| `GET /inventory/{sku}?store_id=` | With `store_id`: `{sku, store_id, qty}`. Without it, or with an empty `store_id=`: `{sku, stores:[{store_id, qty}]}` for all 40 stores, sorted by `store_id`. An unknown SKU or store is a 404 |
| `POST /inventory/adjust` | Body `{sku, store_id, delta:int, reason?:str}`. Applies `delta`. The result may not go below 0, or it is **409** `{"error":"insufficient_stock","detail":…}` and nothing changes. An unknown SKU or store is a 404. Returns `{sku, store_id, qty, delta}`, where `qty` is the new level. Records one `stock_adjust` write |
| `POST /inventory/transfer` | Body `{sku, from_store, to_store, qty:int>0}`. Moves stock atomically. If there isn't enough it is a 409 with the same body as adjust, and neither store changes. An unknown SKU or store is a 404. Returns `{sku, from_store, to_store, qty, from_qty, to_qty}` (the new levels). Records one `stock_transfer` write |
| `POST /refunds` | Body `{order_id, amount:number>0, reason?:str}`. An unknown order is a 404. Inserts a refund, `RF-0001` and upward. The next ID is (refunds since reset) + 1, so a failed refund uses up no ID. Returns **201** `{refund_id, order_id, amount, status:"issued"}`. Records one `refund` write. There is **no** refund-total cap: S3b issues one refund on `CO-1042` every run |
| `GET /refunds/{refund_id}` | The refund row: `{refund_id, order_id, amount, reason, status, created_at}`. `created_at` is ISO-8601 UTC with a `Z` |
| `GET /healthz` | `{"status":"ok","service":"erp-mock"}` |

## Admin API (port 8181, never routed through XC)

| Route | Behavior |
|---|---|
| `GET /admin/state` | `{refunds_issued, stock_writes, last_writes:[{ts, kind, detail, actor}]}`. The counts are since the last reset. `stock_writes` = adjust + transfer. `last_writes` holds the newest 20, newest first. `detail` is stored as JSON text but returned as a parsed object: `{sku, store_id, delta, qty, reason}` for an adjust, the transfer response for a transfer, and `{refund_id, order_id, amount, reason}` for a refund. It never carries a PAN or email |
| `GET /admin/order/{order_id}` | The raw `orders` row with the full PAN: the table's columns (`card_brand`, `card_number`, `card_exp`, …) at the top level, plus `items`, which holds its raw `order_items` rows (`order_id, sku, qty, unit_price`). An unknown order is a 404. The cockpit uses it for S2's "what the ERP really returned" view |
| `POST /admin/reset` | Rebuilds the database from `seed.sql`. Returns `{"status":"reset","refunds_issued":0,"stock_writes":0}`. Must finish in under 2 s |
| `GET /healthz` | `{"status":"ok","service":"erp-mock-admin"}` |

The admin API has **no auth**. Its protection is network placement: it is published on host loopback only. The README and `deploy/aws/core/docker-compose.yaml` both say so. It ignores any `Authorization` header and sends `x-copperline-origin: erp-mock-admin`.

The two apps share no routes. `/admin/*` on the business port is a 401 without a token and a 404 with one, and business routes on the admin port are a 404.

**`actor`** is the service token's `sub`. In the deployed demo every ERP call comes from mcp-servers through `svc-mcp`, so `actor` is always `mcp-servers`. The Estate panel shows *what* changed; which agent and human caused it comes from the cockpit trace and the XC logs.

## Logging

- **Writes.** The business API logs every write as `erp_write`, with `kind` and `actor`, plus:
  - an adjust: `sku`, `store_id`, `delta` and `qty`;
  - a transfer: `sku`, `from_store`, `to_store` and `qty`;
  - a refund: `order_id`, `amount` and `refund_id`.
- **Reads.** Reads log through the standard `http_request` event.
- **Auth denials.** These log as `erp_auth_deny` with `status`, `path` and `reason`, never the token.
- **Service name.** Both apps run in one process, so every log line, admin requests included, has `service: erp-mock`.
- **Never log card numbers or emails.**
