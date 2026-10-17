# services/common: shared library (`copperline_common`)

Every service depends on this package. Keep it boring: plain functions and dataclasses, FastAPI and httpx, no other frameworks. Most service bugs come from getting one of these contracts subtly wrong, so each module has tests in `tests/`.

## MCP: why this is hand-rolled (checked 2026-10-15)

The official MCP Python SDK (`mcp` 2.3.0) implements 2026-07-28. We still use our own small implementation in `mcp.py`, for three reasons:

1. The SDK client always derives `Mcp-Name` from the body, so it cannot send the deliberate header/body mismatch that S1b needs.
2. After a `HeaderMismatch`, the SDK client automatically walks `tools/list`. That would add unscripted calls to the trace.
3. The SDK server answers unmapped JSON-RPC errors, such as our `-32003 Forbidden`, with HTTP 200. Our contract is HTTP 403.

`tests/test_mcp_sdk_interop.py` is dev-only. It proves the official SDK client can discover, list and call our servers. If a later SDK release fixes the three points above, switching is reasonable.

## Modules

| Module | Gives you |
|---|---|
| `config.py` | `config_dir()` (env `CONFIG_DIR`, default `/config`), `config_file(name)`, `load_yaml(path)`. Config files sit flat in one directory, the same as the vK8s ConfigMap mount: `tools.yaml`, `personas.yaml`, `tool-access-matrix.yaml`, `<agent>.yaml` |
| `logging.py` | `setup_logging(service)` and `log_event(event, **fields)`. One JSON object per line on stdout |
| `requestid.py` | Context variables for `request_id`, `agent_id` and `obo_sub`; `current_request_id()`; `RequestIdMiddleware` |
| `service.py` | `setup_service(app, name)`: logging, middleware and `GET /healthz` in one call. Every service calls this |
| `outbound.py` | `new_http_client()`, which propagates `x-request-id`; the `HttpResult` dataclass; and `send()`, which never raises |
| `jwt.py` | RS256 `sign()`, `public_jwks()`, `key_id()`, `unverified_claims()`, `JwksVerifier` |
| `credentials.py` | `secret_env_name(client_id)` and `client_secret(client_id)`. `agent-shopper` maps to env `AGENT_SHOPPER_SECRET` |
| `personas.py` | `Personas.load()`, `base_sub()` (strips the run salt), `customer_id()`, `client()` |
| `tool_schemas.py` | `load_tools()` turns `config/tools/tools.yaml` shorthand into JSON Schema; also `validate_args()` and the model-facing tool names |
| `mcp.py` | MCP 2026-07-28 stateless server (`mcp_router`) and client (`McpClient`) |
| `converse.py` | Helpers for the Bedrock Converse message shape the AI gateway accepts |
| `xcblock.py` | `classify(result, via_lb)` returns `(blocked_by, reason)`; `model_feedback(kind, status, blocked_by, reason)` |
| `tokens.py` | `TokenStore` plus the `client_credentials()` and `exchange()` calls to the IdP |
| `scenarios.py` | Load `scenarios/*.yaml`, match triggers, and expand plans: `${VAR}` first, then `repeat`/`{n}` |
| `plan_executor.py` | `run_plan()`: the **only** implementation of scenario plans (agent-runtime, cockpit, run_scenario) |
| `expect.py` | `evaluate()`: checks a trace against a scenario `expect` block |

### Conventions every service gets from `setup_service`

- **`x-request-id`.** It is read from the request, or generated if missing. It is echoed on the response and added to every outbound call made with `new_http_client()`.
- **`x-copperline-origin: <service>`** is set on **every** response, including errors and 500s. This is how `xcblock` tells a block by our own service (`server`) from a block by the fabric (`xc`). `TODO(verify-xc)`: XC passes origin response headers through unchanged (docs/08 R32).
- **One `http_request` log event per request,** carrying method, path, status and latency. `/healthz` is not logged.
- **Unhandled exceptions** return `500 {"error":"internal_error"}`, still carrying the origin header, and are logged with a traceback.
- **Log fields:** `ts`, `service`, `level`, `request_id`, `agent_id`, `obo_sub`, `event`, `status` and `latency_ms` are always present (null when unknown). Extras are added as top-level keys. Values under keys containing `secret`, `token`, `password`, `authorization`, `api_key`, `api-key`, `cookie` or `private_key` are written as `***` (LLM usage counters such as `inputTokens` are exempt).
- **Identity for logs:** `agent_id` and `obo_sub` are read from the bearer token **without verification**, for logs only.

### `xcblock.classify(result, via_lb)` rules

These are seeded heuristics. Replace them with the real samples in docs/08 during Phase 3.

| Response | `blocked_by` |
|---|---|
| No HTTP response (connect error, DNS failure, timeout) | `network` |
| 2xx or 3xx | `null` |
| Carries `x-copperline-origin` (our service answered) | `server` |
| No origin header, `via_lb=True`, 4xx | `xc` (the fabric or the local-gateway stand-in answered) |
| No origin header, `via_lb=True`, 5xx | `network` (upstream unreachable behind the LB) |
| `via_lb=False` (a URL that is not one of our LBs), any status | `null` |

**The first matching row wins.** So a non-LB URL that never answers is `network` (S5's exfil step expects `status: null, blocked_by: network`), and a non-LB 4xx that carries `x-copperline-origin` is `server`. The last row only covers responses from servers that are not ours.

The `reason` text distinguishes the cases. `local-gateway (NOT XC): …` comes from bodies carrying `x-local-gateway`. `waf` comes from the XC WAF block page signature, `rate limited` from 429, `jwt validation` from 401, and `service policy` from other 403s.

## Minimal MCP 2026-07-28 (what `mcp.py` implements)

### Request

`POST /mcp/<server>`

```
Content-Type: application/json
Accept: application/json, text/event-stream     # servers here always answer application/json
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call | tools/list | server/discover
Mcp-Name: <tool short name>                     # tools/call only
Authorization: Bearer <agent token>
```

```json
{"jsonrpc":"2.0","id":"<uuid>","method":"tools/call",
 "params":{"name":"get_order_status","arguments":{"order_id":"CO-1042"},
           "_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities":{},
                    "io.modelcontextprotocol/clientInfo":{"name":"copperline-agent","version":"1.0"}}}}
```

### Server check order (first failure wins; **nothing after a failed check runs**)

| # | Check | On failure |
|---|---|---|
| 0 | Body is a JSON object with `"jsonrpc":"2.0"` and a string `method` | 400, `-32700 Parse error` or `-32600 Invalid Request` |
| 1a | `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` each appear at most once (an LB and this server could otherwise read different copies) | 400, `-32020 HeaderMismatch`, `data: {"header":<the repeated header>}` |
| 1 | `MCP-Protocol-Version` header is present and, if the body carries `params._meta[…/protocolVersion]`, equals it | 400, `-32020 HeaderMismatch`, `data: {"header":"MCP-Protocol-Version"}` |
| 2 | `Mcp-Method` equals body `method` | 400, `-32020 HeaderMismatch`, `data: {"header":"Mcp-Method"}` |
| 3 | For `tools/call`: `Mcp-Name` is present **and** equals body `params.name`, which must also be present (two missing names do not count as agreement) | 400, `-32020 HeaderMismatch`, `data: {"header":"Mcp-Name"}` (S1b) |
| 4 | Header version is supported (`2026-07-28`) | 400, `-32022 UnsupportedProtocolVersion`, `data: {"supported":[…],"requested":…}` |
| 5 | The service's `authorize` hook: bearer token valid | 401, `-32001 Unauthorized` |
| 5 | The service's `authorize` hook: role allowed (defense in depth) | 403, `-32003 Forbidden` |
| 6 | Method is known | 404, `-32601 Method not found` |
| 7 | `tools/call`: tool exists on this server | 400, `-32602 Invalid params` |
| 8 | `tools/call`: arguments match the input schema | 200, result with `isError: true` (the model can correct itself) |
| 9 | The tool handler raises `ToolError` (e.g. "not your order") | 200, result with `isError: true` |
| 10 | The tool handler raises `UpstreamError` (e.g. ERP down or blocked) | 502, `-32603 Internal error`, `data: {"upstream_status": …}` |

- `GET` and `DELETE` on the MCP path return **405**.
- There are no sessions: no `Mcp-Session-Id` and no `initialize`.
- Every response is `Content-Type: application/json`. Never SSE.

### Results

- **`server/discover`:** `{"supportedVersions":["2026-07-28"],"capabilities":{"tools":{}},"resultType":"complete","ttlMs":60000,"cacheScope":"public","_meta":{"io.modelcontextprotocol/serverInfo":{"name":…,"version":…}}}`
- **`tools/list`:** `{"tools":[{"name","description","inputSchema"}],"resultType":"complete","ttlMs":60000,"cacheScope":"public","_meta":{…serverInfo…}}`
- **`tools/call`:** `{"content":[{"type":"text","text":"<json string>"}],"structuredContent":{…},"isError":false,"resultType":"complete"}`. A tool-level failure has `isError: true` and its message in `content[0].text`.
- **Errors:** `{"jsonrpc":"2.0","id":<id or null>,"error":{"code":…,"message":…,"data":…}}`.

### Client rules (`McpClient`)

1. Send the protocol version header, the `Mcp-*` headers and the `_meta` keys on every request.
2. `header_name_override` sends an `Mcp-Name` that differs from the body. Only safe-mode scenario S1b uses it.
3. Timeout is 10 s per call. No retries on any HTTP response. Retry once on a connection error, never on a timeout.
4. Return the raw `HttpResult`. The caller classifies it with `xcblock`; the client never raises on HTTP status.
