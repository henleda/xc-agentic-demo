# partner-api: Copperline's API for Granite Supply (CE)

Granite Supply is Copperline's supplier. Its own agent (played by the cockpit backend in S6) calls this API through `partner-lb`. Keep it plain: FastAPI, data generated in memory, no database and **no ERP access**.

## Documented operations

These are the only operations in `deploy/xc/openapi/partner-api.yaml` (the XC API definition `apidef-partner`):

| Operation | Behavior |
|---|---|
| `GET /v1/purchase-orders?page=&status=` | 25 POs per page. `page` 1–100 (default 1), `status` `open`, `shipped` or `closed`. Anything else is 422. The data is generated deterministically: `PO-5001` … `PO-5250`, the same on every start and every replica |
| `GET /v1/purchase-orders/{po_id}` | One PO, or 404 |
| `POST /v1/asn` | Body `{po_id, carrier (≤ 32 chars), items: [object]}` → **202** `{asn_id, po_id, status: "accepted"}`. Stored in memory (last 1000) and `po_id` echoed back. There is **no SQL anywhere**: S6(c)'s injection string is about the WAF seeing the payload, not a real vulnerability. A missing field or a too-long carrier is 422 |

## Undocumented operations (shadow endpoints for S6)

Implemented, deliberately **not** in the spec:

- `GET /v1/internal/cost-sheet`: unit costs, list prices, a supplier contact and remit-to bank details. The IBAN is the well-known example IBAN (`DE89 3704 0044 0532 0130 00`): it has a valid format and checksum, so sensitive-data discovery recognises it, but no such account exists.
- `GET /v1/internal/margins`: gross margin by category.

Both log `shadow_endpoint_hit`. In the tenant, XC's OpenAPI validation (fall-through **block**) answers 403 before they are reached, and API discovery has already flagged them as shadow endpoints with sensitive-data tags (S6(b)).

## Auth

Every path except `GET /healthz` needs a partner token. XC validates it at partner-lb; this service checks it **again**, so a request that reaches the pod some other way gets nothing:

| Check | On failure |
|---|---|
| `Authorization: Bearer <jwt>` present | 401 |
| RS256 signature against the IdP's JWKS, `exp`, `iss` (env `ISSUER`), `aud` = `copperline-partner` | 401 |
| claim `role` = `partner` | 403 |
| the keys cannot be loaded | 503 |

Auth runs before routing, so an anonymous caller gets 401 for every path, existing or not. Every response carries `x-copperline-origin: partner-api`.

## Env

| Env | Default | Meaning |
|---|---|---|
| `IDP_BASE_URL` | — | Keys from `${IDP_BASE_URL}/.well-known/jwks.json` (in vK8s `http://idp.agentic-demo`, from the `runtime-env` ConfigMap) |
| `JWKS_PATH` | — | A JWKS file instead. Wins over `IDP_BASE_URL`. With neither, partner-api refuses to start (exit 2) |
| `ISSUER` | `https://idp.copperline.demo` | Required `iss` |
| `PORT` | `8080` | |

It needs no secret. (Until step 8 the vK8s manifest mounted the `svc-mcp` ERP credential; it never used it, so it was removed.)

## Locally vs in the tenant

Locally partner-lb is a local-gateway: routing and JWT only. S6 therefore shows the documented read (200), but the shadow endpoint (200), the injection (202) and the 50-read burst (200s) also succeed. That is the honest local baseline: the 403s and 429s of S6 are XC's OpenAPI validation, WAF and rate limit, which the local stand-in does not emulate (`docs/08` R38). `expect.local` asserts only step i=1.
