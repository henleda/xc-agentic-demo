# copperline-ai-gw: the customer's own AI gateway (EC2, port 8090)

This represents what customers really build: a thin FastAPI or LiteLLM-style proxy that standardizes access to Bedrock. **Keep it ordinary.** Its job in the demo is to be the customer estate that XC governs access to, not to show off.

## API

The contract is in `docs/03-services.md`, so it is not repeated here.

- `POST /v1/converse`
- `GET /v1/usage`
- `POST /v1/usage/reset` (localhost only)
- `GET /healthz`

## Bedrock

- Uses boto3 `bedrock-runtime`, `converse()` with `toolConfig`, through the VPC endpoint. Set `AWS_ENDPOINT_URL_BEDROCK_RUNTIME` only if private DNS is off.
- Credentials come from the instance role.
- Route map env vars: `ROUTE_FAST`, `ROUTE_STANDARD`, `ROUTE_PREMIUM`. Values are Bedrock model IDs or inference-profile IDs. Pick current Anthropic Claude models available in `${AWS_REGION}`, and confirm in the Bedrock console that model access is granted (R24).
- The `premium` route exists only so S5 has something to be denied. It can map to the same model as `standard`.

## Customer-side controls (contrast with XC)

- `x-api-key` check. Normally XC injects it; with `GATEWAY_REQUIRE_KEY=true` it rejects requests that don't have it.
- Per-route daily token budgets. Over budget returns 429 `budget_exceeded`.
- Logs record `x-agent-id`, `x-obo-sub`, `x-request-id`, route, and tokens in and out.

## `DEMO_STUB_MODEL=true`

Returns deterministic canned replies keyed by the `x-request-id` scenario prefix, or a generic reply otherwise. Use it for local dev and as the Bedrock-outage fallback.
