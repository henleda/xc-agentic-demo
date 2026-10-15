# partner-api: Copperline's API for Granite Supply (CE)

## Documented operations

These are the only operations in `deploy/xc/openapi/partner-api.yaml`:

- `GET /v1/purchase-orders?page=&status=`: returns 25 POs per page, generated deterministically.
- `GET /v1/purchase-orders/{po_id}`
- `POST /v1/asn`: returns 202.

## Undocumented operations (shadow endpoints for S6)

These are implemented but not in the spec:

- `GET /v1/internal/cost-sheet`: returns unit costs, margins and supplier bank details (fake IBAN). It feeds sensitive-data discovery.
- `GET /v1/internal/margins`

## Auth

- Bearer token with `aud=copperline-partner`. XC validates it; the service re-checks it.
- `POST /v1/asn` stores items in memory and echoes `po_id` back. It does no SQL at all; the WAF demo is about the payload, not about a real vulnerability.
