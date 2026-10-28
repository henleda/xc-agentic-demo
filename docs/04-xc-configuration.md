# 04 XC configuration

This is the human-readable intent for every XC object. Machine-readable twins live in `deploy/xc/intent/*.yaml`. Claude Code translates them into Terraform (`volterraedge/volterra` provider) under `deploy/xc/terraform/`, checking every field against the provider schema first (see `CLAUDE.md` rule 2).

Field names in backticks come from public docs and Terraform registry pages. They were **not** checked against a live tenant. Treat them as hints.

## Infrastructure objects

| Object | Name | Key settings | TF resource |
|---|---|---|---|
| AWS VPC site | `copperline-aws-ce` | App Stack cluster with **one** interface, in its own subnet ce-site 10.50.4.0/24 whose route table XC creates (App Stack on an AWS VPC site is single-interface, docs/08 R30, R45): origin pools to EC2 use the site-local **outside** network, and `copperline-core-sg` allows ce-site; `m5.2xlarge` (minimum for App Stack), 100 GB disk, single node, label `copperline.demo/site=aws-ce`, offline survivability off (not needed) | `volterra_aws_vpc_site` (see `docs/08` R1 for the SMSv2 alternative) |
| Virtual site | `vs-ce-aws` | Type CE, selector `copperline.demo/site in (aws-ce)` | `volterra_virtual_site` |
| Virtual site | `vs-re-agents` | Type RE, selector on 3 named RE sites close to the presenter. `TODO(verify-xc)`: label key for RE selection | `volterra_virtual_site` |
| vK8s | `agentic-demo-vk8s` | Virtual sites `[vs-re-agents, vs-ce-aws]`; **service isolation off** (so internal LBs work as designed) | `volterra_virtual_k8s` |
| vK8s network policy | `deny-batch-egress` | Applies to pods labelled `tier=batch`: deny all egress except the `idp`, `tools` and `llm` internal LB VIPs | Console or K8s `NetworkPolicy` (`TODO(verify-xc)`: which form vK8s accepts) |
| Secret | `aigw-api-key` | Blindfold-encrypted AI gateway key, used by llm-lb header injection | `volterra` secret policy plus `vesctl` blindfold (`deploy/xc/MANUAL_STEPS.md`) |

## Origin pools

| Pool | Origin type | Target | Site / network | Port |
|---|---|---|---|---|
| `op-shopper` | K8s service on given sites | `svc-shopper-agent.agentic-demo` | `vs-re-agents`, vK8s networks on site | 8080 |
| `op-storeops` | K8s service | `svc-storeops-agent.agentic-demo` | `vs-ce-aws`, vK8s networks | 8080 |
| `op-refunds` | K8s service | `svc-refunds-agent.agentic-demo` | `vs-ce-aws`, vK8s networks | 8080 |
| `op-mcp-catalog` | K8s service | `svc-mcp-catalog.agentic-demo` | `vs-re-agents`, vK8s networks | 8080 |
| `op-mcp-orders` | K8s service | `svc-mcp-orders.agentic-demo` | `vs-ce-aws`, vK8s networks | 8080 |
| `op-mcp-inventory` | K8s service | `svc-mcp-inventory.agentic-demo` | `vs-ce-aws`, vK8s networks | 8080 |
| `op-mcp-refunds` | K8s service | `svc-mcp-refunds.agentic-demo` | `vs-ce-aws`, vK8s networks | 8080 |
| `op-idp` | K8s service | `svc-idp.agentic-demo` | `vs-ce-aws`, vK8s networks | 8080 |
| `op-partner-api` | K8s service | `svc-partner-api.agentic-demo` | `vs-ce-aws`, vK8s networks | 8080 |
| `op-erp` | IP on given sites | EC2 private IP | site `copperline-aws-ce`, inside network | 8081 |
| `op-aigw` | IP on given sites | EC2 private IP | `copperline-aws-ce`, inside network | 8090 |
| `op-cockpit` | IP on given sites | EC2 private IP | `copperline-aws-ce`, inside network | 8095 |

Every pool gets an HTTP health check on `/healthz`, plus outlier detection with defaults. Circuit breaker: max 100 pending requests (S4 talk track: fabric-level backpressure).

## HTTP load balancers

### Public LBs (advertised on the internet from the REs, HTTPS with auto-cert)

| LB | Domain | Routes → pool | Security |
|---|---|---|---|
| `shop-lb` | `shop.<DEMO_DOMAIN>` | `/v1/chat` → `op-shopper`; `/auth/demo-login`, `/.well-known/*` → `op-idp` | WAF `waf-agents` (blocking); JWT validation on `/v1/chat` (aud `copperline-shop`); user identification `uid-jwt-sub`; malicious user detection on; rate limit 30/min per user on `/v1/chat` |
| `ops-lb` | `ops.<DEMO_DOMAIN>` | `/storeops/v1/chat` → `op-storeops` (prefix rewrite to `/v1/chat`); `/refunds/v1/chat` → `op-refunds` (rewrite); `/auth/demo-login`, `/.well-known/*` → `op-idp` | WAF; JWT (aud `copperline-ops`); service policy `sp-ops`: `role=store_associate` → `/storeops/*`, `role∈{care_agent,care_supervisor}` → `/refunds/*`, otherwise deny |
| `partner-lb` | `partner.<DEMO_DOMAIN>` | `/v1/*` → `op-partner-api`; `/auth/token` → `op-idp` | WAF; JWT (aud `copperline-partner`) except `/auth/token`; API definition `apidef-partner` with OpenAPI validation **Block** and fall-through **Block**, scoped to `/v1/*` so `/auth/token` is not blocked as undocumented (R29); API discovery on; sensitive data discovery on; rate limit 20 per 10 s per `sub` |
| `cockpit-lb` | `cockpit.<DEMO_DOMAIN>` | `/*` → `op-cockpit` | Service policy: allow presenter CIDRs only |

### Internal LBs (advertise custom; no internet VIP)

For each, advertise on **"vK8s Service Network on RE"** for `vs-re-agents` **and** on **site `vs-ce-aws`, network "vK8s Service Network"**. `erp-lb` is the exception (CE only). The domain must be `<name>.<namespace>`.

| LB | Domain | Advertised on | Routes → pool | Security |
|---|---|---|---|---|
| `tools-lb` | `tools.agentic-demo` | RE + CE | `/mcp/catalog` → `op-mcp-catalog`; `/mcp/orders` → `op-mcp-orders`; `/mcp/inventory` → `op-mcp-inventory`; `/mcp/refunds` → `op-mcp-refunds` | JWT (aud `copperline-tools`); **service policy `sp-tools` (generated from the access matrix)**; user identification `uid-jwt-agent`; rate limiter `rl-tools`; WAF `waf-agents`; **Data Guard** on `/mcp/orders`; API definition `apidef-mcp` (validation Block, fall-through Block); API discovery and sensitive data discovery on |
| `llm-lb` | `llm.agentic-demo` | RE + CE | `/v1/*` → `op-aigw` | JWT (aud `copperline-llm`); service policy `sp-llm` (generated: `agent_role` × `x-model-route`); user identification `uid-jwt-agent`; rate limit 30/min per agent; **request header add `x-api-key` from secret `aigw-api-key`**; request header remove `x-api-key` from the client (so agents cannot supply their own) |
| `idp-lb` | `idp.agentic-demo` | RE + CE | `/*` → `op-idp` | Service policy: allow `POST /token/exchange`, `POST /auth/token`, `GET /.well-known/*`; deny everything else |
| `erp-lb` | `erp.agentic-demo` | **CE only** | `/*` → `op-erp` | JWT (aud `copperline-erp`, `svc_role=tool-server`); WAF; deny `/admin/*`. **No Data Guard here on purpose**, so S2 shows the masking at the agent hop (tools-lb) |

> **Advertisement check (Phase 3, step 1):** from a debug pod on an RE, confirm that `tools.agentic-demo` resolves and works, and that `erp.agentic-demo` does **not** resolve. If RE pods can't reach internal LBs advertised this way, use the documented "re2ce" pattern from the f5devcentral xchaawsdemoguide and record the change in `docs/08`.

## Generated service policy `sp-tools` (shape)

`scripts/gen_policy.py` turns the access matrix into ordered rules. Rule order is the matrix order, followed by a final default deny.

```
for each agent A, for each tool T in A.tools:
  allow if
    jwt_claims.agent_role == A.agent_role
    AND jwt_claims.obo_role in (T.require_obo_roles or A.allowed_user_roles)
    AND path == tool_servers[T.server].path
    AND header Mcp-Method == "tools/call"
    AND header Mcp-Name == T.short_name
for each agent A, for each server S that A has any tool on:
  allow if POST AND agent_role == A.agent_role AND obo_role in A.allowed_user_roles
        AND path == S.path AND Mcp-Method in (tools/list, server/discover)
default: deny
```

Before generating, check: (1) how many rules fit in a service policy, (2) whether a single rule can AND several header and JWT-claim matchers. If (2) fails, see the fallback in `docs/08`.

## Generated service policy `sp-llm`

For each agent A and each route R in A.model_routes: allow if `POST /v1/converse` (exact path) AND `agent_role == A.agent_role` AND `obo_role in A.allowed_user_roles` AND header `x-model-route == R`. Otherwise deny.

The rule is pinned to `POST /v1/converse` so agents cannot reach other gateway endpoints, such as `GET /v1/usage`, through llm-lb.

## User identification

| Policy | Identifier | Used by |
|---|---|---|
| `uid-jwt-sub` | JWT claim `sub` | shop-lb, ops-lb, partner-lb |
| `uid-jwt-agent` | JWT claim `agent_id` | tools-lb, llm-lb |

## Rate limiting

| Policy | Attached to | Rule |
|---|---|---|
| `rl-tools` | tools-lb | Header `Mcp-Name: adjust_stock` → 10/min per user identifier; default 60/min per user identifier |
| shop-lb inline | shop-lb | `/v1/chat` 30/min per user |
| partner-lb inline | partner-lb | 20 per 10 s per user |
| llm-lb inline | llm-lb | 30/min per user |

## WAF `waf-agents`

- Mode: blocking. Default signature set, high and medium accuracy.
- AI risk-based blocking: on, if available.
- Allow `application/json` and `application/x-www-form-urlencoded` bodies (the IdP token endpoints take form posts). Set a body-size limit that fits MCP payloads.
- Staged signatures: off for the demo, so blocks are immediate.

## API definitions

| Definition | Source | Notes |
|---|---|---|
| `apidef-mcp` | `deploy/xc/openapi/mcp-tools.yaml` | Describes `POST /mcp/{server}` with `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` as required headers. Anything else is fall-through |
| `apidef-partner` | `deploy/xc/openapi/partner-api.yaml` | Documents 3 operations only. partner-api also implements `GET /v1/internal/cost-sheet` and `GET /v1/internal/margins`, which are shadow endpoints |

**Run order for discovery:** run S6(b) traffic once with validation in **Report** mode at least 24 hours before the first rehearsal (2 hours minimum), so the shadow endpoint already shows in the API inventory. Then switch to Block. `TODO(verify-xc)`: how long discovery takes to show new endpoints.

## Logging

- **Request logs and security events:** filter by LB and by user identifier. In the cockpit `.env`, set `XC_CONSOLE_LINKS` to deep links for each scenario.
- **Global log receiver to S3** (bucket `copperline-demo-xc-logs`): optional, for the post-demo "forensics" appendix.
