# 03 Services and interface contracts

Every service in this repo:

- uses Python 3.12 and FastAPI;
- listens on port 8080 (vK8s) or the port listed below (EC2);
- serves `GET /healthz`;
- writes JSON logs;
- propagates `x-request-id`.

Each folder under `services/` has a README with its full contract, and each is built as its own image. Only `common` is not a service: it is shared library code.

| Service | Image | Runs on | K8s Service / host | Purpose |
|---|---|---|---|---|
| `agent-runtime` | `agent-runtime` | RE + CE | `svc-shopper-agent`, `svc-storeops-agent`, `svc-refunds-agent` | One image, three Deployments. Config comes from `config/agents/*.yaml` via ConfigMap |
| `insights-nightly` | `agent-runtime` | CE (CronJob, suspended) | n/a | The shadow agent. Same image, `AGENT_CONFIG=insights-nightly.yaml`, `MODE=batch` |
| `mcp-catalog` | `mcp-servers` | RE | `svc-mcp-catalog` | Public catalog snapshot from `services/mcp-servers/data/catalog.json`, including the poisoned review |
| `mcp-orders` | `mcp-servers` | CE | `svc-mcp-orders` | Wraps ERP order endpoints |
| `mcp-inventory` | `mcp-servers` | CE | `svc-mcp-inventory` | Wraps ERP inventory endpoints |
| `mcp-refunds` | `mcp-servers` | CE | `svc-mcp-refunds` | Wraps the ERP refund endpoints |
| `idp` | `idp` | CE | `svc-idp` | Demo IdP: login, token exchange, client credentials, JWKS |
| `partner-api` | `partner-api` | CE | `svc-partner-api` | REST API for Granite Supply; has a deliberately undocumented internal route |
| `erp-mock` | `erp-mock` | EC2 `copperline-core` | `:8081` | Copperline's system of record (SQLite). Admin endpoints `/admin/state` and `/admin/reset` on `127.0.0.1:8181` only |
| `copperline-ai-gw` | `ai-gw` | EC2 `copperline-core` | `:8090` | The customer's homegrown AI gateway in front of Bedrock |
| `cockpit` | `cockpit` | EC2 `copperline-core` | `:8095` | Presenter UI plus the simulated Granite Supply partner agent |

The `mcp-servers` image runs one server per container, selected by `MCP_SERVER=catalog|orders|inventory|refunds`.

## Key contracts (summary; see each service's README for the full contract)

### copperline-ai-gw: the customer's AI gateway

Copperline built this themselves. It is deliberately ordinary, so it looks like what customers actually run.

- **`POST /v1/converse`**
  - Body: `{ "messages": [...], "tools": [...]?, "system": str?, "max_tokens": int? }`
  - Header `x-model-route: fast|standard|premium` selects a Bedrock model or inference-profile ID from env: `ROUTE_FAST`, `ROUTE_STANDARD`, `ROUTE_PREMIUM`. A request that carries `x-model-route` more than once, or an unknown route, is rejected with 400 `{ "error": "bad_route" }`: llm-lb's service policy matches this header, so the gateway must never act on a copy the fabric did not judge (docs/08 R37).
  - Calls Bedrock Converse through the `bedrock-runtime` VPC endpoint, using the EC2 instance role.
  - Returns the Bedrock Converse response, normalized as `{ "output": ..., "stop_reason": ..., "usage": {...}, "route": str, "model_id": str }`.
- **Authentication.**
  - The gateway requires `x-api-key: <GATEWAY_API_KEY>`.
  - **Agents never hold this key.** XC `llm-lb` injects it with `request_headers_to_add`, sourced from an XC secret.
  - The gateway also logs the `x-agent-id` and `x-obo-sub` headers, which `llm-lb` sets from JWT claims (if XC supports this; see `docs/08`). Otherwise the agent runtime sends them and `llm-lb` only checks that they match the claims.
- **Budgets.** Per-route daily token budgets live in memory (`BUDGET_TOKENS_<ROUTE>`). Over budget, it returns 429 with `{ "error": "budget_exceeded" }`. This is customer logic, which shows that XC and the customer gateway are complementary.
- **`GET /v1/usage`.** Returns tokens by route and by `x-agent-id`. Read by the cockpit. It is served on a separate admin port, **8190**, published on host loopback only, with `POST /v1/usage/reset`, so it is never reachable through llm-lb.
- **`DEMO_STUB_MODEL=true`.** Returns canned responses with no Bedrock call. Use it for local development and as the outage fallback.

### MCP tool servers (2026-07-28 stateless)

- The endpoint is `POST /mcp/<server>`. It is a single path per server, which is how `tools-lb` routes requests by prefix.
- Required headers:
  - `MCP-Protocol-Version: 2026-07-28`
  - `Mcp-Method`
  - `Mcp-Name` (required when the method is `tools/call`)
  - `Authorization: Bearer <agent token>`
- Supported methods: `server/discover`, `tools/list`, `tools/call`.
- Responses are always `application/json` and never SSE.
- **Header/body check:** if `Mcp-Name` does not equal `params.name` in the body, respond with HTTP 400 and JSON-RPC error `{code: -32020, message: "HeaderMismatch"}` (scenario S1b).
- Tool names on the wire are the short names (`issue_refund`). The fully-qualified name used in policy is `<server>.<tool>`.
- For defence in depth, tool servers also check the token's `agent_role` against the matrix. They log a denial as `blocked_by: server`. In the demo this check should never fire, because XC blocks first. If it does fire, the XC policy is wrong.
- `mcp-orders.get_order_details` returns `payment.card_number`, the **full fake PAN** (Luhn-valid test numbers), plus `email`, so that Data Guard has something to mask (S2).
- `mcp-catalog.get_reviews(product_id="TNT-ALPINE-2")` returns review #3, which contains the injection text in `scenarios/data/poisoned-review.txt`.

### agent-runtime

See `docs/02-agents.md` → *Agent runtime behavior*.

### idp

| Endpoint | Purpose | Reachable via |
|---|---|---|
| `POST /auth/demo-login` | `{sub, run_salt?}` → user token, with role taken from `config/personas.yaml` | shop-lb, ops-lb |
| `POST /auth/token` | Client credentials grant, used by service tokens and the partner | partner-lb, idp.agentic-demo |
| `POST /token/exchange` | Agent client ID/secret + user token + `requested_audience` → agent token | idp.agentic-demo only |
| `GET /.well-known/jwks.json` | Public signing keys | all |

- Signing keys are generated once by `scripts/gen_keys.py` and stored as a vK8s Secret.
- The public JWKS is written to `deploy/xc/generated/jwks.json`, which is pasted into or referenced by every LB's JWT validation config.

### erp-mock

- **Seed data:** 40 stores, 60 SKUs, 200 orders, and 3 customers who matter for the demo.

  | Customer | Order | Card on file |
  |---|---|---|
  | Alex Rivera | CO-1042 | Visa test 4111 1111 1111 1111 |
  | Mallory | CO-1077 | — |
  | Pat Gomez | CO-1100 | — |

- **Endpoints:**
  - `GET /orders/{id}`
  - `GET /customers/{id}/orders`
  - `GET /inventory/{sku}`
  - `POST /inventory/adjust`
  - `POST /inventory/transfer`
  - `POST /refunds`
  - `GET /refunds/{id}`
- **Admin endpoints (`127.0.0.1:8181`, never reachable through XC):** `/admin/state` (counts of refunds and stock writes since reset, plus the last 20 writes) and `/admin/reset`.

### cockpit

- **Purpose:** a single-page app the presenter drives. It is served at `https://cockpit.<DEMO_DOMAIN>` behind `cockpit-lb`, with a service policy that allows only the presenter's IP CIDRs.
- **Layout:**
  - **Left:** a persona picker and chat.
  - **Center:** the live agent trace. Each step is a row showing allow (green), `blocked_by: xc` (red), or `server` / `network` (amber), with status code and latency.
  - **Right:** an "Estate" panel showing ERP refunds issued and stock writes since reset, polled from `erp-mock /admin/state`, and AI gateway token usage by agent, from `/v1/usage`.
- **Scenario buttons:** one per scenario. A button pre-fills the persona and message.
- **Deep links:** an "Open in XC" link per scenario, taken from `.env`, goes to the relevant LB's security events and request logs in the XC console.
- **Partner (the S6 button):** the backend plays the Granite Supply agent calling `partner.<DEMO_DOMAIN>` with its client-credentials token. Its four requests (a: purchase orders, b: the internal cost sheet, c: the injected ASN, d: 50 page reads, shown as one summary row) are rows in the trace.
