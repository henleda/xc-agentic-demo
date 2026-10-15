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
| `trigger` | regex | Case-insensitive; matched against the last user message **and** must match `persona` |
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
| `token: {grant, client_id, audience, save_as}` | Get a token from the IdP; one audience per step |
| `repeat` | int. Expands the step N times, with `n` = 1..N |
| `concurrency` | int. Run repeated steps with this much parallelism (default 1) |
| `stop_on_status` | int or null. Stop the plan when this status is seen. The default `null` means never stop |

**Plan length:** scripted plans are **not** bounded by an agent's `max_steps`. `max_steps` applies only to model-driven loops.

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
