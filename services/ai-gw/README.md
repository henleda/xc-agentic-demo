# copperline-ai-gw: the customer's own AI gateway (EC2, port 8090)

This represents what customers really build: a thin FastAPI proxy that standardizes access to Bedrock. **Keep it ordinary.** Its job in the demo is to be the customer estate that XC governs access to, not to show off. XC decides *who* may reach it (llm-lb: JWT, `sp-llm`, rate limit, key injection); the gateway decides *what it costs* (route map, token budgets).

## Processes and ports

The container runs **one** Python process (`python -m ai_gw`) that serves two FastAPI apps sharing one in-memory usage table:

| App | Port | Routes | Reachable from |
|---|---|---|---|
| Gateway | **8090** | `POST /v1/converse`, `GET /healthz` | XC `llm-lb` (`op-aigw`) only. Locally: the local-gateway `llm-lb` |
| Admin | **8190** | `GET /v1/usage`, `POST /v1/usage/reset`, `GET /healthz` | Host loopback only: compose publishes `127.0.0.1:8190:8190`. The cockpit and `reset_demo.sh` use it. XC never routes here, and `sp-llm` only allows `POST /v1/converse` anyway |

The admin API has **no auth**. Its protection is network placement, the same as erp-mock's admin port.

## `POST /v1/converse`

The body and response shapes are in `docs/03-services.md` and `services/common/API.md` (converse section). Checks run in this order; the first failure wins. Every error body is `{"error": str, "message": str}`.

| # | Check | On failure |
|---|---|---|
| 1 | `x-api-key` equals `GATEWAY_API_KEY`, sent exactly once (skipped when `GATEWAY_REQUIRE_KEY=false`) | 401 `invalid_api_key` |
| 2 | Exactly one `x-model-route` header, and it is `fast`, `standard` or `premium` with a model configured (Bedrock mode). A repeated header is refused: llm-lb's `sp-llm` matched one copy, so the gateway must never act on another (docs/08 R37) | 400 `bad_route` |
| 3 | Body: a JSON object; `messages` a non-empty list of `{role: user\|assistant, content: [blocks]}` starting with `user`; optional `system` string; optional `tools` list of `{name, description, input_schema}`; optional positive integer `max_tokens` (capped at 2048) | 400 `bad_request` |
| 4 | The route's daily token budget (`BUDGET_TOKENS_<ROUTE>`) is not used up. Checked before the call, so the last call of the day can overshoot by its own size | 429 `budget_exceeded` |
| 5 | The model call. Bedrock `ThrottlingException` → 429, other Bedrock errors → 502 `bedrock_error`, timeouts → 504, unreachable → 502 `bedrock_unreachable` | as listed |

Refused and failed calls are not counted in usage. Every response carries `x-copperline-origin: ai-gw`.

**Identity headers.** `x-agent-id` and `x-obo-sub` are logged, and usage is counted per `x-agent-id` (`unknown` when absent). The agent runtime sends them. The gateway does not verify them: XC llm-lb has already validated the agent's JWT. `TODO(verify-xc)`: whether llm-lb can set them from JWT claims instead (docs/03).

## `GET /v1/usage` (admin port)

```json
{"day": "2026-10-19", "mode": "stub|bedrock", "calls": 4, "rejected_budget": 0,
 "by_route": {"fast": {"input": 120, "output": 40, "total": 160, "calls": 3}},
 "by_agent": {"shopper": {"input": 120, "output": 40, "total": 160, "calls": 3}},
 "budgets": {"fast": {"limit": 200000, "used": 160}, "standard": {...}, "premium": {...}}}
```

Counts are since the last reset, or 00:00 UTC, or the last restart, whichever is latest. `POST /v1/usage/reset` returns `{"status": "reset"}`.

## Bedrock mode (`DEMO_STUB_MODEL=false`)

- boto3 `bedrock-runtime` `converse()` with the instance role's credentials (never baked into the image), through the VPC endpoint. Set `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` only if private DNS is off.
- `tools` in our wire format become Bedrock's `toolConfig.tools[].toolSpec` (`inputSchema.json`); `system` becomes `[{"text": …}]`; `max_tokens` becomes `inferenceConfig.maxTokens`. The response's `output`, `stopReason` and `usage` are passed through, normalized as `{output, stop_reason, usage, route, model_id}`.
- Route map: `ROUTE_FAST`, `ROUTE_STANDARD`, `ROUTE_PREMIUM` are Bedrock model IDs or inference-profile IDs. Pick current Anthropic Claude models available in `${AWS_REGION}`, and confirm model access in the Bedrock console (R24). A route left empty is refused with `bad_route`.
- The `premium` route exists only so S5 has something to be denied. It can map to the same model as `standard`.

## Stub mode (`DEMO_STUB_MODEL=true`)

No network calls. Token counts are word counts, so usage and budgets still move.

- **Scripted turns.** A request whose `x-demo-scenario` header names a scenario in `ai_gw/stub_replies.yaml` gets that canned reply. Anything else gets `default`. Edit the file freely; only `test_every_scenario_with_a_model_step_has_a_stub_reply` depends on it. (`TODO(verify-xc)` R34: the header must pass through llm-lb. It only matters in stub mode.)
- **Model-driven turns** (no matching scenario, `tools` offered, no `toolResult` yet in the messages): the stub calls the first offered tool whose name shares a word with the last user message, with empty input, and `stop_reason: tool_use`. Once a tool result is in the messages it replies with text. This exists so agent-runtime's tool loop can be tested offline; it is not trying to be smart.

Local compose defaults to stub mode (`LOCAL_STUB_MODEL`, default `true`). On stage, real Bedrock is the default and the stub is the outage fallback.

## Env

| Env | Default | Meaning |
|---|---|---|
| `DEMO_STUB_MODEL` | `false` | `true` for canned replies |
| `GATEWAY_API_KEY` | — | Required when `GATEWAY_REQUIRE_KEY` is true (the default); ai-gw refuses to start without it |
| `GATEWAY_REQUIRE_KEY` | `true` | Check `x-api-key` |
| `AWS_REGION` | — | Required in Bedrock mode |
| `ROUTE_FAST`, `ROUTE_STANDARD`, `ROUTE_PREMIUM` | — | Model IDs; Bedrock mode needs at least one |
| `BUDGET_TOKENS_FAST`, `_STANDARD`, `_PREMIUM` | `200000` | Tokens per UTC day per route |
| `AIGW_PORT` / `AIGW_ADMIN_PORT` | `8090` / `8190` | Listen ports, both on `0.0.0.0` |
| `STUB_REPLIES_PATH` | packaged `stub_replies.yaml` | Override the canned replies |

## Logging

`converse` per successful call (`route`, `model_id`, `scenario`, `tokens_in`, `tokens_out`, `stop_reason`, the identity headers) and `converse_refused` per refusal (`error`, `reason`, `route`). One `aigw_ready` line at startup with the mode, ports, routes and budgets. Message content is never logged.
