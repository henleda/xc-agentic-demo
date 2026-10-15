# Scenario files

There is one YAML per scenario. `services/common/plan_executor.py` is the **single** implementation that runs a plan. It is used by:

- `agent-runtime`, in safe mode and batch mode;
- the cockpit backend, for S6 partner calls;
- `scripts/run_scenario.py`.

That way all three behave identically.

## Schema

### Scenario (top level)

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Unique ID, e.g. `S1` |
| `title` | str | Human-readable title |
| `persona` | str | A `sub` from `config/personas.yaml`, `partner:<client_id>`, or `job:<cronjob name>` |
| `agent` | str | An `agent_id`, or `none` (partner calls) |
| `message` | str | What the cockpit button sends |
| `trigger` | regex | Case-insensitive `re.search` against the last user message. The caller's `sub` must also equal `persona`, after stripping the run salt from salted personas (`mallory.x-ab12` matches `mallory.x`) |
| `plan` | list[Step] | Executed in order. Each `repeat` is expanded before indexing |
| `final_reply` | str or null | `null` means ask the model for the closing reply |
| `repeat_message` | int | Cockpit sends `message` N times (S7) |
| `variants` | list[Scenario] | Sub-scenarios sharing a file (S3a/S3b). Each variant is a full scenario without `variants` |
| `expect` | Expect | See below |

### Step (exactly one of `model`, `tool`, `http`, `token`)

| Field | Meaning |
|---|---|
| `model: {route}` | A real model call through llm-lb. Its output is ignored except for the final reply |
| `tool: <server>.<tool>` | A real MCP call through tools-lb |
| `args` | Tool arguments |
| `args_template` | Like `args`, with `{n}` substituted (1-based) when `repeat` is set. Python `str.format` syntax |
| `header_name_override` | Send this as `Mcp-Name` instead of the body tool name (S1b) |
| `http: {method, url, body?, headers?, auth?}` | Raw HTTP call. `auth: <name>` adds `Authorization: Bearer <token saved under that name>`. `${VAR}` comes from env; `{n}` comes from `repeat` |
| `token: {grant, client_id, audience, save_as, url?}` | Get a token with the client-credentials grant; one audience per step. `url` defaults to `${IDP_BASE_URL}/auth/token`. The client secret comes from env (`agent-storeops` → `AGENT_STOREOPS_SECRET`) |
| `repeat` | int. Expands the step N times, with `n` = 1..N |
| `concurrency` | int. Run repeated steps with this much parallelism (default 1) |
| `stop_on_status` | int or null. Stop the plan when this status is seen. The default `null` means never stop |

**Plan length:** scripted plans are **not** bounded by an agent's `max_steps`. `max_steps` applies only to model-driven loops.

### How a plan runs (`services/common/plan_executor.py`)

- **Substitution order.** `${VAR}` is replaced from env first. A variable with no value fails the plan before any step runs. `{n}` and `{n:03d}` are replaced next, only in repeated steps. No other braces are touched, so JSON-looking strings are safe.
- **Tokens.** Tool steps use the most recent token with audience `copperline-tools`; model steps use the one with `copperline-llm`. In server mode those are the agent's exchanged tokens. A plan `token` step replaces them, which is how S5 runs on machine-mode tokens.
- **Model steps.** Each sends the conversation plus a plain-text summary of the earlier tool results to `/v1/converse`, with no `tools`, and with header `x-demo-scenario: <id>`. A blocked tool shows up in that summary as `DENIED by policy (HTTP 403)`.
- **Final reply.**
  - If `final_reply` is a string, it is the reply.
  - Otherwise, if the last plan step is a model step, its text is the reply.
  - Otherwise, the runtime makes one extra model call. That call is unscripted and has `i: null`.

### Assertion semantics (`services/common/expect.py`)

- **Step selection.** `i` selects the trace step whose `i` field equals that value. It is the plan index, not the trace position.
- **Ranges.** `i_range: [a, b]` is inclusive on both ends.
  - With `count` (± `tolerance`) or `min_count`, it counts the steps that match.
  - With neither, **every** index from `a` to `b` must be present and must match.
- **`blocked_by`** is checked only when the key is present. `blocked_by: null` asserts "not blocked".
- **`status: 200` on a tool step** also requires `is_error` to be false. An MCP tool failure arrives as HTTP 200 with `isError: true` and must not count as success.
- **`body_not_contains`** (and `body_not_contains_alt`) may be a string or a list. It is checked against `result_preview`, and fails if the preview was truncated, so a truncation can never let it pass by accident.
- **An empty or missing expect block** reports as "no expectations" and is not counted as a pass.

### Expect

```yaml
expect:
  tenant:                        # asserted against the real XC tenant
    steps:
      - { i: 2, status: 403, blocked_by: xc }                       # one step by 0-based index after expansion
      - { i_range: [1, 40], status: 200, count: 10, tolerance: 1 }  # count of matching steps in range, +/- tolerance
      - { i_range: [4, 53], status: 429, min_count: 20 }            # at least N matching steps in range
      - { i: 0, status: 200, body_not_contains: ["4111111111111111", "4111 1111 1111 1111"] }
    estate_delta: { refunds_issued: 0, stock_writes: 10 }   # change in ERP /admin/state during this scenario
    tolerance: { stock_writes: 1 }
    front_door: [ { attempt: 1, status: 403, blocked_by: xc } ]  # for repeat_message scenarios
    soft: [ "free text; logged, never fails the run" ]
    xc_log_hint: "for humans; not asserted"
  local: { ... same shape, looser ... }   # deploy/local is NOT XC
```

- `blocked_by` is one of `xc`, `server`, `network`, or `null`.
- `status: null` means no HTTP response at all (connection refused, timeout, DNS failure).

## Repeatability

The definition of done requires three runs in a row.

- `estate_delta` is measured within each scenario, so runs don't accumulate.
- `RUN_SALT` is passed per run to `/auth/demo-login`, which salts personas marked `salted: true`, so malicious-user state never carries over.
- Rate-limit windows are handled by the runner, which orders scenarios so that S4 and S6 run last. Between full runs, it waits until 65 s after the last S4/S6 call.
- The live reset (`reset_demo.sh`) clears estate and rotates the salt in under 60 s. Before a live show, `preflight.sh` adds the rate-limit wait.
