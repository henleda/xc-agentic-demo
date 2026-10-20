# agent-runtime

One image runs three Deployments (shopper, storeops, refunds) and the insights-nightly batch job. All behavior comes from the agent config (`AGENT_CONFIG`). The contract lives in `docs/02-agents.md` under *Agent runtime behavior*; this file adds implementation detail.

## Environment

| Var | Meaning |
|---|---|
| `AGENT_CONFIG` | Path to the agent YAML |
| `AGENT_CLIENT_SECRET` | Client secret for the agent's `client_id` |
| `<CLIENT_ID>_SECRET` | Read by scenario `token` steps through `credentials.client_secret(client_id)`, e.g. `AGENT_STOREOPS_SECRET` for `agent-storeops`. Batch mode needs it, so `deploy/vk8s/40-insights-cronjob.yaml` sets `AGENT_STOREOPS_SECRET` as well as `AGENT_CLIENT_SECRET`, both from the same copied Secret key. Without it the IdP answers 401 `invalid_client` and S5 fails at `i=0` |
| `TOOLS_BASE_URL`, `LLM_BASE_URL`, `IDP_BASE_URL` | Internal LB URLs |
| `MCP_PROTOCOL_VERSION` | `2026-07-28` |
| `DEMO_SAFE_MODE` | Run scenario plans when a trigger matches |
| `SCENARIOS_DIR` | Default `/app/scenarios`; the scenarios are baked into the image |
| `MODE` | `server` (default) or `batch` |
| `TRACE_SINK` | `response` (server mode) or `stdout` (batch) |
| `RUN_SALT` | Optional suffix for per-run uniqueness |
| `MODEL_TIMEOUT_S` | Default 65. How long the agent waits for llm-lb. It must be **longer** than llm-lb's own route timeout (locally 60 s; in XC see `docs/08` R39), so a slow model shows up as llm-lb's or ai-gw's 504 in the trace, never as an agent-side timeout. Tool and IdP calls use 10 s |
| `PORT` | Default 8080 |

Start it with `python -m agent_runtime`. In server mode it refuses to start without `AGENT_CLIENT_SECRET` or any of the three LB URLs; it also refuses an agent config that declares a tool missing from `tools.yaml`.

## Server mode

`POST /v1/chat` (body and response shapes in `docs/02-agents.md`). A missing bearer token is 401; a malformed body (no messages, more than 50, a non-string content over 8000 characters, or a last message that is not from the user) is 400 `bad_request`.

1. Read the user token from `Authorization`. The token has already been validated by XC at shop-lb or ops-lb.
2. Exchange it at `${IDP_BASE_URL}/token/exchange` for two tokens, one with audience `copperline-tools` and one with `copperline-llm`. Cache them per `(obo_sub, aud)` until 30 seconds before expiry.
3. Pick a path:
   - **Safe mode, trigger matched:** execute the scenario `plan` step by step (see `scenarios/README.md`).
   - **Otherwise, the normal loop:**
     1. Call the model at `${LLM_BASE_URL}/v1/converse` with header `x-model-route` and tool definitions taken from `declared_tools`. The over-declaration is intentional.
     2. Run the tool calls the model returns.
     3. Repeat until the model stops or `max_steps` model calls have been made. At the limit the agent stops and says so.
     4. Tool names: the model sees `orders__get_order_status` (Bedrock allows no dots) and the runtime maps it back. A tool the model names that is **not declared** is never called: the trace shows it with `reason: "not a declared tool"`.
     5. Each tool result goes back to the model as a `toolResult` block. A blocked or failed call is sent as one line from `xcblock.model_feedback`, e.g. "Tool call denied by policy (HTTP 403).", with `status: "error"`, so the model can recover (CLAUDE.md rule 9).
   - **Closing call on scripted turns.** If the plan has no `final_reply` and does not end with a model step, the runtime makes one more model call (`i: null`, `scripted: false`). It sends the conversation plus a one-line-per-tool summary of the plan's results, **no tools**, and the header `x-demo-scenario: <id>`, so ai-gw's stub can return that scenario's canned reply. S3a and S3b end this way.
4. Every outbound call carries:
   - `Authorization: Bearer <agent token>`
   - `x-request-id`
   - `x-agent-id`
   - `x-obo-sub`

   `x-agent-id` and `x-obo-sub` are informational, for the customer gateway's logs (see `docs/08` R9).
5. Wrap every call in a trace Step and classify the outcome with `copperline_common.xcblock`.
6. Return `{reply, scenario, trace}`. `scenario` is the matched scenario ID, or null for the normal loop.

**Token cache.** Agent tokens are cached per `(obo_sub, aud)` until 30 s before expiry, so one human's token is never used for another. Each real exchange is a trace step (`kind: token`, `i: null`, with `identity`); a cache hit adds none. A refused exchange is a trace step too, and the calls that needed that token go out without one, so llm-lb or tools-lb shows its own 401.

## Batch mode (insights-nightly)

1. Run the scenario whose `persona` is `job:<agent_id>`. Its own `token` steps get the client-credentials tokens: no human is involved, so they carry `obo_sub=none` and `obo_role=none`. The runtime does not fetch a token beforehand, so those token steps are what the trace shows at `i=0` and `i=1`.
2. Run the rest of the plan with those tokens.
3. Write each Step as a JSON line to stdout, prefixed with `TRACE `, as it completes. The cockpit tails them through the vK8s API (pod logs). `TODO(verify-xc)`: vK8s supports `kubectl logs`. If it doesn't, post the trace to `cockpit-lb` over the internet instead.
4. Exit 0 even when steps are blocked. Blocked steps are the expected outcome. Only a broken setup exits 2: no scenario with persona `job:<agent_id>`, or a plan that does not expand.

Locally: `deploy/local/compose.sh run --rm insights-nightly`. `EXFIL_TEST_URL` points at a name that does not resolve, standing in for the vK8s network policy.

## Network errors

For `http` plan steps, a connection refusal or timeout is recorded as `status: null, blocked_by: "network"`. The S5 exfil step expects this.
