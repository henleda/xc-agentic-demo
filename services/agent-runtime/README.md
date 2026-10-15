# agent-runtime

One image runs three Deployments (shopper, storeops, refunds) and the insights-nightly batch job. All behavior comes from the agent config (`AGENT_CONFIG`). The contract lives in `docs/02-agents.md` under *Agent runtime behavior*; this file adds implementation detail.

## Environment

| Var | Meaning |
|---|---|
| `AGENT_CONFIG` | Path to the agent YAML |
| `AGENT_CLIENT_SECRET` | Client secret for the agent's `client_id` |
| `TOOLS_BASE_URL`, `LLM_BASE_URL`, `IDP_BASE_URL` | Internal LB URLs |
| `MCP_PROTOCOL_VERSION` | `2026-07-28` |
| `DEMO_SAFE_MODE` | Run scenario plans when a trigger matches |
| `SCENARIOS_DIR` | Default `/app/scenarios`; the scenarios are baked into the image |
| `MODE` | `server` (default) or `batch` |
| `TRACE_SINK` | `response` (server mode) or `stdout` (batch) |
| `RUN_SALT` | Optional suffix for per-run uniqueness |

## Server mode

`POST /v1/chat`

1. Read the user token from `Authorization`. The token has already been validated by XC at shop-lb or ops-lb.
2. Exchange it at `${IDP_BASE_URL}/token/exchange` for two tokens, one with audience `copperline-tools` and one with `copperline-llm`. Cache them per `(obo_sub, aud)` until 30 seconds before expiry.
3. Pick a path:
   - **Safe mode, trigger matched:** execute the scenario `plan` step by step (see `scenarios/README.md`).
   - **Otherwise, the normal loop:**
     1. Call the model at `${LLM_BASE_URL}/v1/converse` with header `x-model-route` and tool definitions taken from `declared_tools`. The over-declaration is intentional.
     2. Run the tool calls the model returns.
     3. Repeat until the model stops or `max_steps` is reached.
4. Every outbound call carries:
   - `Authorization: Bearer <agent token>`
   - `x-request-id`
   - `x-agent-id`
   - `x-obo-sub`

   `x-agent-id` and `x-obo-sub` are informational, for the customer gateway's logs (see `docs/08` R9).
5. Wrap every call in a trace Step and classify the outcome with `copperline_common.xcblock`.
6. Return `{reply, trace}`.

## Batch mode (insights-nightly)

1. Get a token with the client-credentials grant. That gives `obo_sub=none` and `obo_role=none`.
2. Run the scenario plan.
3. Write each Step as a JSON line to stdout, prefixed with `TRACE `. The cockpit tails them through the vK8s API (pod logs). `TODO(verify-xc)`: vK8s supports `kubectl logs`. If it doesn't, post the trace to `cockpit-lb` over the internet instead.
4. Exit 0 even when steps are blocked. Blocked steps are the expected outcome.

## Network errors

For `http` plan steps, a connection refusal or timeout is recorded as `status: null, blocked_by: "network"`. The S5 exfil step expects this.
