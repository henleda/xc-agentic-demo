# erp-mock: Copperline's system of record (runs on EC2, port 8081)

- **Stack:** SQLite, with the seed in `seed.sql`. It is rebuilt on `/admin/reset`.
- **Auth:** requires a service token with `svc_role=tool-server` and `aud=copperline-erp`. XC erp-lb also enforces this.
- **Data:** the seed data customers and orders are listed in `docs/03-services.md`. Card numbers are standard **test PANs** only.

## Endpoints

- `GET /orders/{id}`: returns `customer`, `email`, `items`, `total`, `status`, `payment {brand, card_number, exp}`.
- `GET /customers/{id}/orders`: use `*` for all customers.
- `GET /inventory/{sku}?store_id=`
- `POST /inventory/adjust`: body `{sku, store_id, delta, reason}`.
- `POST /inventory/transfer`
- `POST /refunds`: body `{order_id, amount, reason}`, returns `{refund_id}`.
- `GET /refunds/{id}`

## Admin endpoints

- Served on port **8181** inside the container (bind `0.0.0.0`). The compose file publishes it on the host as `127.0.0.1:8181` only, so it is never reachable through XC.
- `GET /admin/state` returns `{refunds_issued, stock_writes, last_writes:[...]}`.
- `GET /admin/order/{id}` returns the raw row, including the full PAN. The cockpit uses it for the S2 side-by-side view.
- `POST /admin/reset`.
- The cockpit reads these. It runs on the same host.
