# mcp-servers

One image; select the server with `MCP_SERVER=catalog|orders|inventory|refunds`. Every server implements the MCP rules in `services/common/README.md`.

| Server | Backend | Tools |
|---|---|---|
| catalog | `data/catalog.json` (baked in; RE has no ERP access) | `search_products`, `get_product`, `get_reviews` |
| orders | ERP via `${ERP_BASE_URL}` with a service token (`svc-mcp`, aud `copperline-erp`) | `get_order_status` (status, ETA, last 4 of card only), `get_order_details` (**full card number + email**, for Data Guard in S2), `list_customer_orders` |
| inventory | ERP | `check_stock`, `adjust_stock`, `transfer_stock` |
| refunds | ERP | `issue_refund`, `get_refund_status` |

## Check order (mandatory; S1b depends on it)

1. Protocol checks: `MCP-Protocol-Version`, then `Mcp-Method` vs body `method`, then `Mcp-Name` vs body `params.name`. Any mismatch → HTTP 400, JSON-RPC `-32020 HeaderMismatch`. **Nothing else runs.**
2. Defense-in-depth role check (below) → 403 `-32003`.
3. Object-level authorization (below) → HTTP 200 with `isError: true`.
4. Execute the tool.

## Object-level authorization (app logic, not XC)

`orders.get_order_status` and `orders.get_order_details`: when `obo_role=customer`, return `isError: true` ("not your order") unless the order belongs to the persona's `customer_id`. The agent token does not carry `customer_id`; the server maps `obo_sub` → `customer_id` from `config/personas.yaml` (strip any `-<salt>` suffix first). XC can authorize *which tool* an agent calls for *which human*, but it cannot bind a body argument (`order_id`) to a claim (`obo_sub`). That is the deck's "claim-to-body binding" gap. Mention it in Act 5; it is not a separate scenario.

## Defense-in-depth check

Decode the agent token (it has already been validated by XC) and check `agent_role` / `obo_role` against `config/policy/tool-access-matrix.yaml`, which is mounted read-only.

- If the check fails, return a JSON-RPC error `-32003 Forbidden` with HTTP 403.
- Log the event as `server_authz_deny`.
- In a correct deployment this never fires, because XC denies first. The smoke test asserts that it is never seen.

## `data/catalog.json`

Contains 60 products. Product `TNT-ALPINE-2` has 5 reviews; review index 2 is the text of `scenarios/data/poisoned-review.txt`, which the build copies in.
