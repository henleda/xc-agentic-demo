# copperline_common API contract

This is the Python-level contract for `services/common/copperline_common/`. Read it alongside `README.md`, which explains the why. Services and tests depend on **exactly** these names and signatures. If you change one, update this file in the same commit.

Package layout: `services/common/pyproject.toml` (name `copperline-common`, Python 3.12), `services/common/copperline_common/*.py`, `services/common/tests/test_*.py`.

Runtime dependencies are `fastapi`, `httpx`, `pyjwt[crypto]` and `pyyaml`. Tests use `pytest` and FastAPI's `TestClient`. Async tests run through `asyncio.run(...)` inside a sync test, so there is no pytest-asyncio.

## config.py

```python
def config_dir() -> Path                  # Path(os.environ.get("CONFIG_DIR", "/config"))
def config_file(name: str) -> Path        # config_dir() / name
def load_yaml(path: str | Path) -> Any    # yaml.safe_load; FileNotFoundError propagates
def repo_root() -> Path | None            # nearest ancestor of this file containing config/policy/tool-access-matrix.yaml, else None (dev/test helper only)
```

## logging.py

```python
def setup_logging(service: str, level: str | None = None) -> None
    # Root logger: one JSON object per line to stdout. level from arg, then env LOG_LEVEL, default "info". Idempotent.
def log_event(event: str, *, level: str = "info", **fields) -> None
```

Every line carries `ts` (ISO-8601 UTC with milliseconds and a `Z` suffix), `service`, `level`, `request_id`, `agent_id`, `obo_sub`, `event`, `status` and `latency_ms`. Missing values are `null`. `request_id`, `agent_id` and `obo_sub` default to the current context-variable values. Extra fields become top-level keys.

Redaction applies to any key whose lowercased name contains `secret`, `token`, `password`, `authorization`, `api_key`, `api-key`, `cookie` or `private_key`. LLM usage counters (`inputTokens`, `outputTokens`, `totalTokens`, `max_tokens`, `tokens_in`, `tokens_out`, `tokens_total`) are exempt. Its value becomes `"***"`, recursively inside dicts.

## requestid.py

```python
REQUEST_ID_HEADER = "x-request-id"
request_id_var: ContextVar[str | None]; agent_id_var; obo_sub_var
def current_request_id() -> str           # current value, or generate a uuid4 hex, set it, return it
def new_request_id(prefix: str | None = None) -> str   # "<prefix>-<uuid4hex>" or "<uuid4hex>"
class RequestIdMiddleware                 # pure ASGI middleware (not BaseHTTPMiddleware)
```

`RequestIdMiddleware(app, service: str)` handles each HTTP request in order:

1. **Request ID.** Read `x-request-id`, or generate one, and set `request_id_var`.
2. **Identity.** If there is an `Authorization: Bearer <jwt>` header, decode it without verification and set `agent_id_var` and `obo_sub_var` from the claims `agent_id` and `obo_sub`. If the token has no `agent_id`, use `sub` for `obo_sub` instead (that is a user token). Ignore decode errors.
3. **Response headers.** Add `x-request-id` and `x-copperline-origin: <service>` to every response start.
4. **Errors.** An unhandled exception becomes `500 application/json {"error":"internal_error"}`, carrying both headers, and is logged as `unhandled_exception` with the traceback.
5. **Logging.** Log `http_request` with `method`, `path`, `status` and `latency_ms`, except for `GET /healthz`.
6. **Reset.** Clear the context variables after the request.

## service.py

```python
def setup_service(app: FastAPI, service: str) -> None
    # setup_logging(service); add RequestIdMiddleware(service=...); add GET /healthz -> 200 {"status":"ok","service":service}
```

## outbound.py

```python
@dataclass
class HttpResult:
    status: int | None                    # None = no HTTP response
    headers: dict[str, str]               # lowercased keys
    body_text: str
    json: Any | None                      # parsed body if it is valid JSON, else None
    latency_ms: int
    error: str | None = None              # "connect_error: …", "dns: …", "timeout", "invalid_url: …", "<ExcType>: …" when status is None

def new_http_client(timeout: float = 10.0, **kwargs) -> httpx.AsyncClient
    # event hook adds x-request-id (current_request_id()) to every request unless already set
async def send(client: httpx.AsyncClient, method: str, url: str, *, retry_connect_once: bool = False, **kwargs) -> HttpResult
    # never raises for HTTP status, any httpx.HTTPError (transport, DecodingError, TooManyRedirects, …) or InvalidURL;
    # returns status=None + error instead ("<ExcType>: <message>" for errors that are not timeout/connect/dns).
    # retry_connect_once: retry exactly once on httpx.ConnectError (not on timeouts)
```

## jwt.py

```python
def load_private_key(pem: str | bytes) -> RSAPrivateKey
def key_id(public_key) -> str             # stable: base64url(sha256(DER SubjectPublicKeyInfo))[:16]
def public_jwks(private_key) -> dict      # {"keys":[{"kty":"RSA","use":"sig","alg":"RS256","kid":…,"n":…,"e":…}]}
def sign(claims: dict, private_key, *, ttl_s: int, issuer: str, now: int | None = None) -> str
    # adds iss, iat, exp (= now + ttl_s), jti (uuid4 hex) unless present; header kid = key_id(public)
def unverified_claims(token: str) -> dict # no verification; {} on any decode error
class JwtError(Exception)
class JwksVerifier:
    def __init__(self, jwks: dict | None = None, *, jwks_path: str | None = None, jwks_url: str | None = None, issuer: str | None = None)
    def verify(self, token: str, audience: str | list[str] | None = None) -> dict
        # RS256 only; checks signature (by kid), exp, iss (if issuer set), aud (if given). Raises JwtError(reason).
        # Loads keys lazily: jwks dict, else file, else URL (httpx, sync). Re-fetches once on unknown kid.
def bearer_token(authorization_header: str | None) -> str | None   # "Bearer x" -> "x" (case-insensitive scheme)
```

## credentials.py

```python
def secret_env_name(client_id: str) -> str     # "agent-shopper" -> "AGENT_SHOPPER_SECRET"; "svc-mcp" -> "SVC_MCP_SECRET"; "granite-supply" -> "GRANITE_SUPPLY_SECRET"
def client_secret(client_id: str) -> str | None  # os.environ.get(secret_env_name(client_id))
```

## personas.py

```python
@dataclass
class Personas:
    personas: list[dict]; clients: list[dict]
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Personas"   # default config_file("personas.yaml")
    def get(self, sub: str) -> dict | None          # exact sub match on base personas
    def base_sub(self, sub: str) -> str             # "mallory.x-ab12" -> "mallory.x" ONLY for salted personas; else unchanged
    def resolve(self, sub: str) -> dict | None      # get(base_sub(sub))
    def customer_id(self, sub: str) -> str | None   # resolve(sub).get("customer_id")
    def client(self, client_id: str) -> dict | None
def salted_sub(sub: str, run_salt: str | None) -> str   # f"{sub}-{run_salt}" if run_salt else sub
```

`base_sub` only strips the suffix when the text before the **last** `-` is a salted persona's sub. `alex.rivera-x` stays `alex.rivera-x`, because Alex isn't salted.

## tool_schemas.py

```python
@dataclass(frozen=True)
class ToolDef:
    server: str; name: str; description: str; input_schema: dict
    @property
    def fq(self) -> str                   # "orders.get_order_status"
    @property
    def model_name(self) -> str           # "orders__get_order_status"  (Bedrock names: ^[a-zA-Z0-9_-]{1,64}$)
def load_tools(path: str | Path | None = None) -> dict[str, ToolDef]   # keyed by fq; default config_file("tools.yaml")
def tools_for_server(tools: dict[str, ToolDef], server: str) -> list[ToolDef]
def fq_from_model_name(model_name: str) -> str   # "orders__get_order_status" -> "orders.get_order_status"
def validate_args(tool: ToolDef, args: dict) -> str | None   # None if ok, else human message
```

The shorthand maps `string` to `{"type":"string"}`, and does the same for `integer`, `number` and `boolean`. A trailing `?` makes the argument optional; all others go in `required`. Every schema is `{"type":"object","properties":{…},"required":[…],"additionalProperties":false}`.

In `tools.yaml`, an optional type inside a flow mapping (`{ … }`) **must be quoted**, as in `limit: "integer?"`. YAML reserves `?` in flow context, so PyYAML's pure-Python `SafeLoader` and Ruby's Psych reject an unquoted `integer?`. libyaml's `CSafeLoader` happens to accept it, so a file can look fine in one tool and still crash `load_tools()`. Block style (`limit: integer?` on its own line) needs no quotes. `load_tools` does not work around a file that won't parse: the parse error propagates and the service fails at startup.

`validate_args` checks required keys, unknown keys and basic types. An `integer` must be an int and not a bool; a `number` may be an int or float but not a bool.

## mcp.py

```python
PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS = ("2026-07-28",)
HEADER_MISMATCH = -32020; UNSUPPORTED_PROTOCOL_VERSION = -32022
UNAUTHORIZED = -32001; FORBIDDEN = -32003
PARSE_ERROR = -32700; INVALID_REQUEST = -32600; METHOD_NOT_FOUND = -32601; INVALID_PARAMS = -32602; INTERNAL_ERROR = -32603
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

class ToolError(Exception)                # tool-level failure → 200 isError:true; str(e) is the message
class UpstreamError(Exception):           # backend failure → 502 -32603
    def __init__(self, message: str, upstream_status: int | None = None)
class McpDenied(Exception):               # raised by authorize hooks
    def __init__(self, http_status: int, code: int, message: str)   # e.g. (403, FORBIDDEN, "Forbidden")

@dataclass
class McpRequestContext:
    method: str; tool: str | None; token: str | None; claims: dict; request_id: str

ToolHandler = Callable[[dict, McpRequestContext], Awaitable[dict | list | str]]
# The return value becomes structuredContent (a dict; lists/str are wrapped as {"result": value}), and json.dumps of it becomes content[0].text.

def mcp_router(*, server_name: str, path: str, tools: list[ToolDef], handlers: dict[str, ToolHandler],
               authorize: Callable[[McpRequestContext], Awaitable[None]] | None = None,
               server_version: str = "1.0") -> APIRouter
    # handlers keyed by short tool name. authorize() raises McpDenied to reject. Implements the check order in README.md
    # exactly; header comparison is case-insensitive on header NAMES, exact on values. Logs mcp_reject / tool_call events.

def jsonrpc_error(id, code: int, message: str, data: Any = None) -> dict

class McpClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient, *, client_name: str = "copperline-agent", client_version: str = "1.0")
    async def call_tool(self, server: str, tool: str, arguments: dict, *, token: str | None,
                        header_name_override: str | None = None, extra_headers: dict | None = None) -> HttpResult
    async def list_tools(self, server: str, *, token: str | None) -> HttpResult
    async def discover(self, server: str, *, token: str | None) -> HttpResult
    def url(self, server: str) -> str     # f"{base_url}/mcp/{server}"
    # POST with the headers and _meta described in README.md; send(..., retry_connect_once=True)

def tool_result_text(result: HttpResult) -> str | None   # content[0].text of a tools/call result, else None
def tool_result_is_error(result: HttpResult) -> bool | None  # result.isError if a JSON-RPC result, else None
def jsonrpc_error_of(result: HttpResult) -> dict | None  # the "error" object if present
```

## converse.py

These are the message shapes the AI gateway accepts on `POST /v1/converse`. They follow the Bedrock Converse API, passed through unchanged:

- `messages: [{"role":"user"|"assistant","content":[{"text":…}|{"toolUse":{…}}|{"toolResult":{…}}]}]`
- `system: str`
- `tools: [{"name","description","input_schema"}]`
- `max_tokens: int`

Gateway responses are `{"output":{"message":{"role":"assistant","content":[…]}}, "stop_reason":…, "usage":{"inputTokens","outputTokens","totalTokens"}, "route":…, "model_id":…}`.

```python
def text_message(role: str, text: str) -> dict
def chat_to_converse(messages: list[dict]) -> list[dict]
    # [{"role","content": str}] -> converse messages; merges consecutive same-role messages; drops empty;
    # ensures the first message is "user" (prepends a "(conversation start)" user message if needed)
def reply_text(converse_response: dict | None) -> str | None      # joined text blocks of output.message.content
def tool_uses(converse_response: dict | None) -> list[dict]       # [{"toolUseId","name","input"}]
def tool_specs(tools: list[ToolDef]) -> list[dict]                # [{"name": t.model_name, "description", "input_schema"}]
```

## xcblock.py

```python
ORIGIN_HEADER = "x-copperline-origin"
LOCAL_GATEWAY_HEADER = "x-local-gateway"
def classify(result: HttpResult, *, via_lb: bool) -> tuple[str | None, str | None]   # (blocked_by, reason), rules in README.md
def model_feedback(kind: str, status: int | None, blocked_by: str | None, reason: str | None) -> str
    # e.g. "Tool call denied by policy (HTTP 403)." / "Rate limited by the platform (HTTP 429)." /
    #      "Tool call rejected by the tool server (HTTP 400: HeaderMismatch)." / "Network error: no response."
```

## tokens.py

```python
@dataclass
class TokenResult:
    token: str | None; claims: dict; http: HttpResult
    @property
    def expires_at(self) -> int           # claims["exp"] or 0

async def client_credentials(http, url: str, client_id: str, client_secret: str | None, audience: str) -> TokenResult
    # POST form grant_type=client_credentials, client_id, client_secret, audience → {"access_token",…}
async def exchange(http, idp_base_url: str, *, client_id: str, client_secret: str | None, subject_token: str,
                   requested_audience: str) -> TokenResult
    # POST {idp}/token/exchange form grant_type=urn:ietf:params:oauth:grant-type:token-exchange, subject_token,
    # subject_token_type=urn:ietf:params:oauth:token-type:jwt, client_id, client_secret, requested_audience

class TokenStore:
    # In-memory cache keyed by (obo_sub, aud). get() returns a token only if exp - 30 s > now.
    def __init__(self, skew_s: int = 30, clock: Callable[[], float] = time.time)
    def get(self, obo_sub: str, aud: str) -> str | None
    def put(self, obo_sub: str, aud: str, token: str, exp: int) -> None
```

Token claims are read with `unverified_claims`. The agent trusts the IdP response because the hop goes through an XC LB.

## scenarios.py

```python
@dataclass
class PlanStep:
    i: int                                # plan index AFTER repeat expansion (0-based)
    kind: str                             # "token" | "model" | "tool" | "http"
    spec: dict                            # the original step dict with ${VAR} and {n} substituted (repeat/concurrency removed)
    n: int | None                         # 1-based repeat counter, None if not repeated
    group: int                            # index of the source step in the YAML plan (steps from one repeat share it)
    concurrency: int                      # from the source step, default 1
    stop_on_status: int | None

@dataclass
class Scenario:
    id: str; title: str; persona: str; agent: str; message: str; trigger: str
    plan: list[dict]; final_reply: str | None; expect: dict; repeat_message: int | None; source: str
    def matches(self, *, agent_id: str, message: str, sub: str, personas: Personas) -> bool
        # agent == agent_id AND personas.base_sub(sub) == persona AND re.search(trigger, message, re.I)

class PlanError(Exception)

def load_scenarios(directory: str | Path | None = None) -> list[Scenario]
    # default env SCENARIOS_DIR or "/app/scenarios"; every *.yaml sorted by filename; a file with `variants`
    # yields one Scenario per variant (variant fields override file-level fields; variants inherit title if absent).
def find_scenario(scenarios, *, agent_id: str, message: str, sub: str, personas: Personas, hint: str | None = None) -> Scenario | None
    # first match in load order; if hint is given only that id may match
def by_id(scenarios, scenario_id: str) -> Scenario | None

def substitute_env(value: Any, env: Mapping[str, str]) -> Any
    # recursively replaces ${NAME} in strings; missing or empty NAME → PlanError("unresolved ${NAME}")
def substitute_n(value: Any, n: int) -> Any
    # recursively replaces only {n} and {n:<format_spec>} (e.g. {n:03d}) in strings; other braces untouched;
    # a spec that cannot format an int (e.g. {n:03s}) → PlanError("bad {n} format spec '03s': …")
def expand_plan(plan: list[dict], env: Mapping[str, str]) -> list[PlanStep]
    # for each source step: substitute_env first; then if repeat=R expand to R steps with n=1..R and substitute_n
    # (args_template becomes args); validates exactly one of model/tool/http/token → PlanError otherwise
```

## plan_executor.py

```python
@dataclass
class Step:                                # one trace row; field meanings in docs/02-agents.md
    n: int; i: int | None; kind: str; target: str
    tool: str | None = None; mcp_name_header: str | None = None; args_preview: str | None = None
    status: int | None = None; blocked_by: str | None = None; reason: str | None = None
    is_error: bool | None = None; result_preview: str | None = None; result_truncated: bool = False
    identity: dict | None = None; latency_ms: int = 0; scripted: bool = True; request_id: str | None = None
    def to_dict(self) -> dict

PREVIEW_LIMIT = 4000

@dataclass
class ExecContext:
    http: httpx.AsyncClient
    tools_base_url: str | None
    llm_base_url: str | None
    idp_base_url: str | None
    tokens: dict[str, str]                 # saved tokens by name; also "aud:copperline-tools" / "aud:copperline-llm"
    agent_id: str | None = None
    obo_sub: str | None = None
    lb_base_urls: tuple[str, ...] = ()     # extra LB URLs; targets starting with these, or with tools/llm/idp_base_url, are via_lb=True
    conversation: list[dict] = field(default_factory=list)   # chat messages [{"role","content"}]
    system_prompt: str | None = None
    scenario_id: str | None = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    model_route_default: str | None = None
    step_hook: Callable[[Step], None] | None = None   # called after each step completes (cockpit/batch streaming)

@dataclass
class PlanRun:
    steps: list[Step]; reply: str | None; stopped_early: bool

async def run_plan(scenario: Scenario, ctx: ExecContext, *, start_n: int = 1) -> PlanRun
def tool_summary(earlier: list[Step]) -> str | None
    # the "Results of the tool calls you made:" text model steps get (see below); None when there are no tool steps.
    # agent-runtime reuses it for its closing call, so both say the same thing.
```

`run_plan` behaves like this:

- **Setup.** It calls `expand_plan(scenario.plan, ctx.env)` and runs steps in order. Consecutive steps from one `group` with `concurrency > 1` run in batches of that size with `asyncio.gather`. The resulting steps are sorted by `i`, and `n` is assigned sequentially from `start_n`.
- **`token` step.**
  - Calls `client_credentials` with `url` = `spec.url` or `${idp_base_url}/auth/token`, and with the secret from `credentials.client_secret(client_id)`.
  - On success, saves the token under `save_as` and under `"aud:<audience>"`.
  - The trace Step has `kind: "token"` and `identity`, holding `agent_id`, `agent_role`, `obo_sub`, `obo_role`, `aud` and `sub` from the claims (keys absent from the claims are omitted).
  - `target` is the URL. It is never a secret: no secret ever appears in `args_preview` or `result_preview`.
- **`tool` step.**
  - Uses `McpClient(ctx.tools_base_url)` and the token `"aud:copperline-tools"`, applying `header_name_override` if set.
  - `tool` is the fully-qualified name; `mcp_name_header` is set only when the override differs.
  - `is_error` comes from `tool_result_is_error`.
  - `result_preview` is the tool text if there is one; otherwise the raw body.
- **`model` step.**
  - POSTs `${llm_base_url}/v1/converse` with the token `"aud:copperline-llm"`.
  - Headers: `x-model-route: <spec.model.route>` and `x-demo-scenario: <scenario.id>`, plus `x-agent-id` and `x-obo-sub` when known.
  - Body: `{"system": ctx.system_prompt, "messages": chat_to_converse(ctx.conversation + [summary]), "max_tokens": 400}`. The summary is one user message: "Results of the tool calls you made:" followed by one line per earlier tool step, `- <fq tool> -> <status>: <first 1500 chars of result_preview or model_feedback(...)>`. Use `model_feedback(...)` whenever the step has a `blocked_by`, even if a raw body such as "Forbidden" exists, so a denial reads "denied by policy (HTTP 403)" (scenarios/README.md). Use the preview only for unblocked steps. A missing status prints as `no response`. With no earlier tool steps there is no summary message and `"system"` is left out when `system_prompt` is `None`.
  - No `tools` are sent.
  - `result_preview` is the reply text.
- **`http` step.**
  - Sends `method` to `url`. A `body` is sent as JSON. `headers` are added. `auth: name` adds `Authorization: Bearer <tokens[name]>`.
  - If the token name is missing, the step records `status: null`, `blocked_by: null` and `reason: "no token saved as <name>"`.
- **Every step.**
  - Classified with `classify(result, via_lb=target startswith any ctx.lb_base_urls)`. Steps of kind tool and model are always `via_lb=True`.
  - `args_preview` is `json.dumps(args or body)`, capped at 300 characters.
  - `result_preview` is capped at `PREVIEW_LIMIT`, with `result_truncated` set accordingly.
  - The `request_id` sent on the call is generated per step as `new_request_id(scenario.id)`.
- **Stopping.** `stop_on_status` stops the plan once a step's status equals it; later steps are skipped and `stopped_early=True`.
- **Reply.**
  - If `scenario.final_reply` is a str, that is the reply.
  - Otherwise, if the last executed step is a model step with text, that text is the reply.
  - Otherwise `reply=None`. The caller (agent-runtime) makes the extra unscripted model call only when the last executed step is **not** a model step; if the plan's own final model step failed or returned no text, the turn ends without a second try (a failed model call is never retried as a "closing" call).
- **Errors.** `run_plan` never raises for HTTP or network problems. It raises only `PlanError` for bad plans or unresolved variables, before any step runs.

## expect.py

```python
@dataclass
class Check:
    ok: bool; description: str; detail: str
@dataclass
class ExpectResult:
    ok: bool; checks: list[Check]; skipped: bool         # skipped = no steps/estate_delta/front_door (soft/xc_log_hint alone don't count); ok=False
def select_block(expect: dict | None, env: str) -> dict | None     # expect.get(env)
def evaluate(block: dict | None, trace: list[dict], *, estate_delta: dict | None = None) -> ExpectResult
```

The semantics are those in `scenarios/README.md` under "Assertion semantics", plus four details:

- **Step matching.** A step matches a spec when every key present among `status`, `blocked_by` and `is_error` equals the step's value. If `status == 200` and the step kind is `tool`, `is_error` must also be `false` (a parsed MCP result with `isError: false`). `is_error: null` means no MCP result came back (an HTML block page, an empty body, a JSON-RPC error), so the check fails with "status 200 but no MCP result".
- **Estate.**
  - `estate_delta: {k: v}` compares against the observed `estate_delta[k]`, allowing ± `tolerance[k]` (default 0).
  - If `estate_delta` was expected but no observation was passed in, that is a failed check: "estate not observed".
- **Front door.** `front_door` items assert on `trace` entries whose `kind == "front_door"` and whose `attempt` field matches.
- **Soft.** Each `soft` item becomes a Check with `ok=True` and a `"(soft) "` description prefix. It never fails.
