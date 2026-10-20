# 02 Agents, tools and identity

## Agent matrix

| Agent ID | Role claim | Runs on | Users | Model routes allowed | Tools allowed (enforced by XC) | Notes |
|---|---|---|---|---|---|---|
| `shopper` | `shopper` | RE (`vs-re-agents`), 3 replicas, one per RE | customers | `fast` | `catalog.search_products`, `catalog.get_product`, `catalog.get_reviews`, `orders.get_order_status` | Its config **over-declares** `refunds.issue_refund` on purpose (see "Why one agent is misconfigured") |
| `storeops` | `storeops` | CE (`vs-ce-aws`) | store associates | `fast`, `standard` | `catalog.*`, `inventory.check_stock`, `inventory.adjust_stock`, `inventory.transfer_stock`, `orders.get_order_status` | Writes inventory; target of the runaway-loop scenario |
| `refunds` | `refunds` | CE | care agents, care supervisors | `standard` | `orders.get_order_details`, `orders.list_customer_orders`, `refunds.issue_refund` (supervisors only), `refunds.get_refund_status` | Highest privilege; on-behalf-of rule enforced at the fabric |
| `insights-nightly` | *(none registered)* | CE, CronJob | nobody: a shadow deployment | none | none | Uses a copied `storeops` client secret; with no human behind it (`obo_role=none`) every tool and model call is denied, and egress is blocked |
| `granite-supply` | `partner` | Outside Copperline (simulated by the cockpit backend over the internet) | Granite Supply Co. | n/a | Partner API only: `GET /v1/purchase-orders`, `GET /v1/purchase-orders/{id}`, `POST /v1/asn` | Third-party agent; Copperline governs it but does not host it |

The authoritative version of this table is `config/policy/tool-access-matrix.yaml`. This doc is the human-readable copy. If the two disagree, the YAML wins.

## Personas (demo users)

| Persona | `sub` | `role` | Logs in to | Typical agent |
|---|---|---|---|---|
| Alex Rivera | `alex.rivera` | `customer` | shop | shopper |
| Mallory (attacker) | `mallory.x` | `customer` | shop | shopper |
| Jordan Lee | `jordan.lee` | `store_associate` | ops | storeops |
| Sam Patel | `sam.patel` | `care_agent` | ops | refunds |
| Morgan Chen | `morgan.chen` | `care_supervisor` | ops | refunds |
| Granite Supply (client credentials, not a login) | `granite-supply` | `partner` | partner-lb `/auth/token` | granite-supply |

## Identity model

There are four token types, all RS256 JWTs signed by the demo IdP (`services/idp`). They share one signing key, and its public JWKS is **pasted into each XC LB's JWT validation config**, so XC never has to fetch keys at request time.

1. **User token.** Issued by `/auth/demo-login` to the cockpit.
   - Claims: `iss`, `sub`, `role`, and `aud` ∈ {`copperline-shop`, `copperline-ops`}. (The partner gets a client-credentials token with `aud=copperline-partner`, not a user token.)
   - Lifetime: 30 minutes.
2. **Agent token.** Issued by `/token/exchange`. This is an RFC 8693-style exchange: the agent's client credentials plus the user token, with `requested_audience` ∈ {`copperline-tools`, `copperline-llm`}.
   - Claims, all flat so XC JWT-claim matchers can use them:

     | Claim | Value |
     |---|---|
     | `sub` | the agent ID |
     | `agent_id` | the agent ID |
     | `agent_role` | the agent's role |
     | `obo_sub` | the user's `sub` |
     | `obo_role` | the user's `role` |
     | `aud` | the requested audience |
     | `jti` | unique token ID |

   - Lifetime: 5 minutes.
   - The IdP refuses an exchange if the user's `role` is not in the agent's `allowed_user_roles`.
3. **Machine-mode agent token.** An agent client using the client-credentials grant at `/auth/token`, with no user involved, gets the same claim set but with `obo_sub=none` and `obo_role=none`. Real IdPs allow this, and it is how the shadow agent (S5) gets a token. Every generated XC rule requires `obo_role ∈ allowed_user_roles`, so machine-mode tokens are useless at the tool and model hops unless the matrix grants them explicitly.
4. **Service token.** Client credentials only, for tool server → ERP.
   - Claims: `sub` = service name, `svc_role` = `tool-server`, `aud` = `copperline-erp`.

Why flat claims: XC service-policy JWT-claim matching on nested objects such as `act.sub` is unverified. Flat string claims are the safe choice. `TODO(verify-xc)`: can one rule AND two claim matchers (`agent_role` and `obo_role`)? Scenario S3 depends on it. The fallback is in `docs/08`.

## Why one agent is misconfigured

In `config/agents/shopper.yaml`, `declared_tools` includes `refunds.issue_refund`, as if a developer copy-pasted from the refunds agent. `scripts/gen_policy.py` flags this as `OVERDECLARED` and is told to allow it via `demo_overdeclare: true`. This sets up scenario S1. Even when the agent code and the model are both wrong, the fabric holds, because XC policy comes from the access matrix and not from agent code.

## Agent runtime behavior (shared by all agents)

- One image, `agent-runtime`. Its behavior comes from `AGENT_CONFIG` (path to a mounted YAML).
- **API:** `POST /v1/chat`, with the user token in `Authorization`.
  - Request body: `{ "messages": [{"role": "user" | "assistant", "content": str}, ...], "scenario_hint": str? }`.
  - `scenario_hint` narrows safe-mode matching to one scenario ID. The trigger and persona must still match.
- **Response body:** `{ "reply": str, "scenario": str | null, "trace": [Step] }`. Each Step is:

  ```
  {
    "n": int,                 # 1-based position in this trace (display order)
    "i": int | null,          # plan index after repeat expansion; null for unscripted steps.
                              #   Scenario expect blocks address steps by this field.
    "kind": "token" | "model" | "tool" | "http",
    "target": str,            # URL called, e.g. http://tools.agentic-demo/mcp/refunds
    "tool": str?,             # fully-qualified tool, e.g. refunds.issue_refund
    "mcp_name_header": str?,  # only when the Mcp-Name header differs from the body (S1b)
    "args_preview": str?,     # JSON of the arguments, truncated
    "status": int | null,     # null = no HTTP response at all
    "blocked_by": "xc" | "server" | "network" | null,
    "reason": str?,
    "is_error": bool?,        # tool steps: the MCP result's isError
    "result_preview": str?,   # response text: tool content text, model reply, or raw body
    "result_truncated": bool, # true when result_preview was cut
    "identity": {...}?,       # token steps: agent_id, agent_role, obo_sub, obo_role, aud of the minted token
    "latency_ms": int,
    "scripted": bool,
    "request_id": str
  }
  ```

  The trace includes the agent's own token exchanges as `kind: "token"` steps with `i: null`, but only when an exchange actually happened (not on a cache hit).
- **Loop:**
  1. Exchange the user token for tools and llm tokens. Cache each per human; serve a cached token only while it has at least 150 s of its 300 s left, so it cannot expire mid-turn.
  2. Call the model with the declared tools.
  3. Execute the tool calls.
  4. Repeat, up to `max_steps` (default 8). Scripted plans are not bounded by `max_steps` (`scenarios/README.md`).
- Model calls go to `http://llm.agentic-demo/v1/converse` with header `x-model-route: <route>`.
- Tool calls follow MCP 2026-07-28 stateless Streamable HTTP. They go to `http://tools.agentic-demo/mcp/<server>` with headers `Mcp-Method: tools/call` and `Mcp-Name: <tool>`.
- `DEMO_SAFE_MODE=true`: if the last user message matches a `trigger` in any `scenarios/*.yaml` targeting this agent, the runtime runs that scenario's `plan` (model and tool steps) instead of asking the model what to do. It still asks the model for the final natural-language reply, unless `plan.final_reply` is set.
- On a 401, 403 or 429 from an XC LB, recognise the XC block page or JSON and set `blocked_by: "xc"`. Then tell the model "Tool call denied by policy (HTTP 403)" and let it recover.
  - Every Copperline service stamps its responses with `x-copperline-origin`. A 4xx from an LB that lacks this header came from the fabric. See `services/common/README.md`, under `xcblock`.
  - `TODO(verify-xc)`: capture the exact block response format for service-policy deny, WAF block and rate-limit 429, so the runtime can tell them apart. Record samples in `docs/08`.
