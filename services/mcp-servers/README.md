# mcp-servers

One image runs four MCP servers. `MCP_SERVER=catalog|orders|inventory|refunds` picks which one a container runs. Every server follows the MCP 2026-07-28 rules in `services/common/README.md` and is built with `copperline_common.mcp.mcp_router`. Do not hand-roll MCP here.

| Server | Path | Backend | Tools |
|---|---|---|---|
| catalog | `/mcp/catalog` | `data/catalog.json`, baked into the image (it runs on the RE, which has no ERP access) | `search_products`, `get_product`, `get_reviews` |
| orders | `/mcp/orders` | The ERP via `${ERP_BASE_URL}`, using a service token | `get_order_status`, `get_order_details`, `list_customer_orders` |
| inventory | `/mcp/inventory` | The ERP | `check_stock`, `adjust_stock`, `transfer_stock` |
| refunds | `/mcp/refunds` | The ERP | `issue_refund`, `get_refund_status` |

Tool names and input schemas come from `${CONFIG_DIR}/tools.yaml` (`copperline_common.tool_schemas.load_tools`). This README defines what each tool **returns**.

## Process

- **Start command.** `python -m mcp_servers` starts uvicorn on `0.0.0.0:${PORT:-8080}`. It serves the selected server's MCP route plus `GET /healthz`, set up with `copperline_common.service.setup_service(app, "mcp-<server>")`.
- **Other paths** get a 404, including other servers' paths.
- **Startup checks.** The process refuses to start when:
  - `MCP_SERVER` is unknown;
  - an ERP-backed server has no `ERP_BASE_URL`, `IDP_BASE_URL` or `SVC_CLIENT_SECRET`;
  - `tools.yaml` lacks one of the server's tools.

## Check order (mandatory; S1b depends on it)

The order is the one in `services/common/README.md`. This server adds the `authorize` hook and the tool handlers. In short:

1. **Protocol checks.** Duplicate routing headers, `MCP-Protocol-Version`, then `Mcp-Method` against the body `method`, then `Mcp-Name` against the body `params.name`. Any mismatch → HTTP 400, JSON-RPC `-32020 HeaderMismatch`. **Nothing else runs**: no token decode, no role check, no ERP call.
2. **`authorize` hook** (below): token present → else 401 `-32001`. Role allowed → else 403 `-32003`.
3. **Argument validation** against `tools.yaml` → 200 with `isError: true`.
4. **Tool handler.** This includes object-level authorization, which returns 200 with `isError: true`.

## `authorize` hook: defense in depth

XC has already validated the JWT (tools-lb JWT profile) and enforced `sp-tools`. This hook re-checks the same decision from the same source of truth, so a broken fabric policy is caught by the server, which logs `server_authz_deny`.

1. **Token present.** No bearer token → `McpDenied(401, -32001, "Unauthorized")`.
2. **Claims.** Read them with `copperline_common.jwt.unverified_claims`. The server does **not** verify the signature: XC (or the local-gateway) is the verifier, and the JWKS is not mounted on RE pods. A token that won't decode, or lacks `agent_role` or `obo_role`, also gets 401.
3. **Discovery.** For `tools/list` and `server/discover`, allow if the matrix gives `agent_role` any tool on this server **and** `obo_role` is in that agent's `allowed_user_roles`. Otherwise deny with 403.
4. **`tools/call`.** Find the matrix entry for `agent_role` and fully-qualified tool `<server>.<tool>`. Allow only if it exists **and** `obo_role` is in the entry's `require_obo_roles` (falling back to the agent's `allowed_user_roles`). Otherwise `McpDenied(403, -32003, "Forbidden")`.
5. **Machine-mode tokens** (`obo_role: "none"`) are denied unless the matrix lists `"none"` explicitly, which it never does.
6. **Logging.** Every denial is logged as `server_authz_deny`, with `agent_role`, `obo_role`, `tool` and `reason`.

The matrix is read from `${CONFIG_DIR}/tool-access-matrix.yaml`, using the same `norm_tool_entry` semantics as `scripts/gen_policy.py` (a plain string, or `{tool, require_obo_roles}`). Keep the two in agreement; a test compares them.

**In the demo this hook should never fire, because XC denies first.** It fires locally only when a request bypasses the local-gateway. The smoke test asserts that `server_authz_deny` is absent after a full scenario run.

## Object-level authorization (app logic, not XC)

For `orders.get_order_status` and `orders.get_order_details` when `obo_role == "customer"`:

1. **Map the human to a customer.** Map `obo_sub` to a `customer_id` with `Personas.customer_id(obo_sub)`. That strips a run salt first, so `mallory.x-ab12` maps to `C-1066`.
2. **Compare.** If the order's `customer_id` differs, or the persona has none, raise `ToolError("not your order")`, which is 200 with `isError: true`.
3. **Leak nothing.** The message must not reveal whether the order exists, so an unknown order gets the same message for a customer.

For any other `obo_role`, such as `care_agent`, there is no object-level restriction.

This is the deck's "claim-to-body binding" gap. XC can authorize *which tool* an agent calls for *which human*, but it can't bind a body argument (`order_id`) to a claim (`obo_sub`). Mention it in Act 5; it is not a separate scenario.

## ERP access (orders, inventory, refunds)

- **Service token.** Each server gets a token with `copperline_common.tokens.client_credentials` at `${IDP_BASE_URL}/auth/token`:
  - `client_id` from `SVC_CLIENT_ID` (default `svc-mcp`);
  - `client_secret` from `SVC_CLIENT_SECRET`;
  - `audience` `copperline-erp`.
- **Caching.** The token is cached until 30 s before `exp`. A 401 from the ERP triggers exactly one refresh and retry. The token is never logged.
- **Client.** Calls use `copperline_common.outbound.new_http_client()`, so `x-request-id` propagates, with a 10 s timeout.
- **Error mapping.**

  | ERP answer | Tool outcome |
  |---|---|
  | 2xx | Success |
  | 404 | `ToolError("<thing> not found")` |
  | 409 (insufficient stock) | `ToolError("insufficient stock: <detail>")` |
  | 422 | `ToolError("invalid request: <first message>")` |
  | Any other status, or no response | `UpstreamError("ERP unavailable", upstream_status)` → 502 |

  In particular, an `erp-lb` block (401/403 from XC) surfaces as **502**. That is honest: it is a broken backend from the agent's point of view.

## Tool results

Each tool returns a dict that becomes `structuredContent`, with the same JSON in `content[0].text`.

| Tool | Returns |
|---|---|
| `catalog.search_products(query, limit?)` | `{query, results:[{sku, name, category, price}]}`. A case-insensitive substring match on name, category and description. `limit` defaults to 10 (maximum 25) |
| `catalog.get_product(product_id)` | `{sku, name, category, price, description, review_count, average_rating}`. Unknown → `ToolError("product not found")` |
| `catalog.get_reviews(product_id)` | `{product_id, reviews:[{rating, title, author, verified, text}]}`. Reviews are returned **verbatim**, including the poisoned review for `TNT-ALPINE-2`. The tool server does not sanitize content: S1's point is that prompt-level inspection is not the LB's job |
| `orders.get_order_status(order_id)` | `{order_id, status, placed_at, eta, items:[{sku, name, qty}], payment_last4}`. **No email, no full PAN, no totals** (masked by the app) |
| `orders.get_order_details(order_id)` | The ERP order **unmodified**: `{order_id, customer_id, customer, email, status, placed_at, eta, items, total, payment:{brand, card_number, exp}}`. The full PAN is intended; S2's Data Guard masks it at tools-lb |
| `orders.list_customer_orders(customer_id)` | `{customer_id, count, orders:[…full ERP orders…]}`. `*` returns all of them (S5's target) |
| `inventory.check_stock(sku, store_id?)` | The ERP response unchanged |
| `inventory.adjust_stock(sku, store_id, delta, reason?)` | The ERP response unchanged. `reason` defaults to `"agent adjustment"` |
| `inventory.transfer_stock(sku, from_store, to_store, qty)` | The ERP response unchanged |
| `refunds.issue_refund(order_id, amount, reason?)` | The ERP response (`{refund_id, order_id, amount, status}`). `reason` defaults to `"agent refund"` |
| `refunds.get_refund_status(refund_id)` | The ERP refund row |

**Logging:** one `tool_call` event per call (from `mcp_router`), with `tool`, `agent_role`, `obo_sub` and outcome. Tool arguments may be logged, but **results are never logged**, because they hold PANs and emails.

## Environment

| Var | Default | Used by |
|---|---|---|
| `MCP_SERVER` | (required) | all |
| `PORT` | `8080` | all |
| `CONFIG_DIR` | `/config` | all (`tools.yaml`, `tool-access-matrix.yaml`, `personas.yaml`) |
| `CATALOG_PATH` | the `data/catalog.json` packaged next to the code | catalog |
| `ERP_BASE_URL` | — | orders, inventory, refunds |
| `IDP_BASE_URL` | — | orders, inventory, refunds |
| `SVC_CLIENT_ID` / `SVC_CLIENT_SECRET` | `svc-mcp` / — | orders, inventory, refunds |

## `data/catalog.json`

It holds the same 60 products as the ERP. `scripts/gen_seed_data.py` generates it alongside `services/erp-mock/erp_mock/seed.sql`, from the same product list, and it is committed. `make check` fails if either file is stale.

- **Product shape:** `{sku, name, category, price, description, reviews:[{rating:int 1-5, title, author, verified:bool, text}]}`.
- **`TNT-ALPINE-2`** ("Alpine 2 Tent", 549.00) has exactly 5 reviews.
- **Review index 2** is the injection review for S1. Its `text` is the full contents of `scenarios/data/poisoned-review.txt`, byte for byte; a drift test checks this.
- **Every other product** has 0–4 benign reviews.
