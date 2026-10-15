# services/common: shared library (`copperline_common`)

## Modules to build

- `logging.py`: a JSON log formatter. Fields: `ts`, `service`, `level`, `request_id`, `agent_id`, `obo_sub`, `event`, `status`, `latency_ms`, plus any extras.
- `requestid.py`: FastAPI middleware. It reads or creates `x-request-id`, stores it in a contextvar, and adds it to every outbound httpx call.
- `jwt.py`: RS256 sign and verify (PyJWT). `verify(token, aud)` checks against the JWKS file or URL in `JWKS_PATH` / `IDP_BASE_URL`. Services use this for defense-in-depth checks only; XC is the primary enforcement point.
- `mcp.py`: a minimal MCP **2026-07-28** stateless Streamable HTTP implementation. Use the official SDK instead if it supports this revision (check first).
- `plan_executor.py`: the **only** implementation of scenario plans (see `scenarios/README.md`). It is used by agent-runtime (safe and batch modes), the cockpit backend (S6) and `scripts/run_scenario.py`.
- `xcblock.py`: classifies an HTTP response as an XC block and returns `(blocked_by, reason)`. It is seeded with heuristics. Replace them with the real samples recorded in the table in `docs/08`.

## Minimal MCP 2026-07-28 spec (enough for this demo)

### Request

`POST /mcp/<server>`

Headers:

```
Content-Type: application/json
Accept: application/json
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call | tools/list | server/discover
Mcp-Name: <tool short name>          # required for tools/call
Authorization: Bearer <agent token>
```

Body:

```json
{"jsonrpc":"2.0","id":"<uuid>","method":"tools/call","params":{"name":"get_order_status","arguments":{"order_id":"CO-1042"}}}
```

### Server rules

1. **Protocol version.** A missing or unsupported `MCP-Protocol-Version` gets HTTP 400.
2. **Method check.** If `Mcp-Method` does not equal `method` in the body, return HTTP 400 with the JSON-RPC error `{"code":-32020,"message":"HeaderMismatch","data":{"header":"Mcp-Method"}}`.
3. **Name check.** For `tools/call`, if `Mcp-Name` does not equal `params.name`, return the same error with `"header":"Mcp-Name"`. This is scenario S1b.
4. **`tools/list`.** Return the tools for this server, taken from `config/tools/tools.yaml`.
5. **`server/discover`.** Return `{name, version, protocolVersions:["2026-07-28"], capabilities:{tools:{}}}`.
6. **Results.**
   - Success: `{"jsonrpc":"2.0","id":...,"result":{"content":[{"type":"text","text":"<json string>"}],"structuredContent":{...},"isError":false}}`.
   - Tool-level failure (for example, order not found): `isError: true` with HTTP 200.
7. **Content type.** Always respond with `Content-Type: application/json`. Never use SSE.
8. **No sessions.** No `Mcp-Session-Id` and no initialize handshake.

### Client rules

1. Send the protocol version header and the `Mcp-*` headers on every request.
2. Support `header_name_override`, used only by safe-mode scenario S1b, to send an `Mcp-Name` that differs from the body.
3. Timeout is 10 s per call. No retries on 4xx. Retry once on a connection error.
