# Phase 1 close-out: services, locally

Date: 2026-10-23. Built and checked on the devbox (EC2 `copperline-devbox`, `deploy/devbox/`), with `make check` also run on a Mac. Everything here runs under `deploy/local/docker-compose.yaml`, which is **not XC**: the local-gateway stands in for the load balancers (docs/08 R38).

## Acceptance (docs/07, Phase 1)

| Check | Result |
|---|---|
| `make check` passes | **Pass.** 1672 passed, 36 skipped (Mac); the skips are the live-stack and Chrome tests, run separately below |
| `run_scenario.py --all --env local` passes every scenario with an `expect.local` block | **Pass**, three runs in a row (`RUNS=3 make local-scenarios`, stub model, tightened S0): 27/27 passed, `results/scenarios-20261023T185959Z.json` on the devbox. S7 has no local expectations (WAF and malicious-user detection are XC's) |
| S0 works against real Bedrock on the devbox | **Blocked: Bedrock model access**, not code. See below |

Also run on the devbox: `make local-check` (35 live tests against the 18 running containers), `make local-smoke` (every hop answered by the service behind its LB), and every cockpit button through the tunnel.

### S0 against real Bedrock: blocked on model access (docs/08 R24)

What was done, on 2026-10-23, from the devbox instance role (`copperline-devbox-role`):

1. `aws bedrock list-inference-profiles` lists every current Claude profile as ACTIVE in us-east-1. Planned routes: fast `us.anthropic.claude-haiku-5-5` (shopper), standard `us.anthropic.claude-sonnet-5-5` (refunds, storeops), premium `us.anthropic.claude-opus-5-5` (insights-nightly).
2. One Converse call per model with ai-gw's exact request (maxTokens 400, one tool) succeeded: a `toolUse` block in about 1 s, no thinking blocks crowding the 400-token cap.
3. The stack then came up with `LOCAL_STUB_MODEL=false` and those `ROUTE_*` (passed on the command line; there is no `.env` on the devbox). `GET 127.0.0.1:8190/v1/usage` reported `"mode": "bedrock"`.
4. S0's model step returned **502**: ai-gw logged `bedrock_error AccessDeniedException`, "not authorized to perform the required AWS Marketplace actions (aws-marketplace:ViewSubscriptions, aws-marketplace:Subscribe)". From then on every call to those models was denied. Five other models (Haiku 4.5, Sonnet and Opus 4.6, Sonnet and Opus 5) answered their first call and were denied five minutes later.

So each model answers once while Bedrock tries to subscribe the account to it on AWS Marketplace, and that subscription fails because the role may not subscribe. The fix is an account step, not code: an admin completes the model subscriptions once (Bedrock console, Model access), or the calling role gets `aws-marketplace:Subscribe` and `aws-marketplace:ViewSubscriptions` for the first call. The EC2 core role (Phase 0 step 3) needs the same. Then re-run:

```bash
make devbox-run CMD="LOCAL_STUB_MODEL=false ROUTE_FAST=us.anthropic.claude-haiku-5-5 \
  ROUTE_STANDARD=us.anthropic.claude-sonnet-5-5 ROUTE_PREMIUM=us.anthropic.claude-opus-5-5 make local-up"
make devbox-run CMD="uv run python scripts/run_scenario.py S0 --env local"
make devbox-run CMD="make local-up"          # back to stub mode
```

S0 must PASS, and ai-gw's `converse` log line must show the real `model_id`.

**This run found a hole, now fixed.** S0 passed with the 502, because its `expect.local` asserted only step 1. It now asserts all three steps like `expect.tenant`, so a failed model hop fails S0 (`scenarios/S0-baseline.yaml`; `test_real_s0_block_fails_when_the_model_hop_fails` fails without the change). This is a tightening; no expect block was loosened.

## What was built

| Component | Where | Notes |
|---|---|---|
| Shared library | `services/common` (`copperline_common`) | JSON logs, request IDs, JWT/JWKS, MCP 2026-07-28 client and server, xcblock, plan executor, expect evaluator, demo driver. Contract: `services/common/API.md` |
| IdP | `services/idp` | Demo login, RFC 8693 token exchange with role checks, client credentials, JWKS |
| ERP mock | `services/erp-mock` | Seed data, `/admin/state` and reset on a loopback admin port |
| MCP servers | `services/mcp-servers` | catalog, orders, refunds, inventory: one image, four servers; plain JSON, never SSE |
| AI gateway | `services/ai-gw` | Bedrock Converse or stub mode; per-route daily token budgets; usage on loopback 8190 |
| Agent runtime | `services/agent-runtime` | Chat API, OBO token exchange cached per user and audience, scripted plans in safe mode, model loop, batch mode for insights-nightly |
| Partner API | `services/partner-api` | Three documented operations and two shadow endpoints for S6; 64 KiB body cap |
| Cockpit | `services/cockpit` | The presenter page: scenario buttons, live trace, Estate panel. Same driving and judging code as the runner |
| Local stand-in for XC | `deploy/local/local-gateway` | One container per XC LB except cockpit-lb, reading `deploy/xc/intent/`: routes, JWT validation, service policy, header changes. Nothing else (no WAF, Data Guard, rate limits) |
| Compose | `deploy/local/docker-compose.yaml`, `compose.sh` | 18 containers, hardened like vK8s: arbitrary UID, read-only root, tmpfs `/tmp`, no capabilities |
| Scripts | `scripts/` | `gen_keys.py`, `gen_policy.py`, `render_secrets.py`, `gen_seed_data.py`, `run_scenario.py`, `smoke_test.py` (both `--env local`) |
| Devbox | `deploy/devbox` | EC2 dev box over SSM; `make devbox-run`, `devbox-tunnel`, `devbox-results` |

Each step had a review round (reviewers plus adversarial skeptics); confirmed findings were fixed with tests that fail without the fix. The commit history has one commit per step and per review.

## Deviations from the spec

- **MCP is hand-rolled**, not the official SDK (CLAUDE.md rule 6). The SDK (`mcp` 2.3.0) supports 2026-07-28, but cannot send S1b's deliberate header/body mismatch, adds unscripted `tools/list` calls after a mismatch, and answers our `-32003 Forbidden` with HTTP 200. A dev-only interop test proves the SDK client works against our servers. Reasons in `services/common/README.md`.
- **Trace `Step` has more fields** than docs/02 first listed: `i`, `kind: http`, `is_error`, `result_preview`/`result_truncated`, `identity`, `method`. docs/02 is updated.
- **Scenario plans use URL variables** (`${PARTNER_BASE_URL}` and friends), so one plan runs locally and in the tenant.
- **Internal-LB gateways listen on port 80 locally** (`ip_unprivileged_port_start=0`), so services use tenant URLs unchanged. vK8s workloads still listen on 8080.
- **Expect blocks tightened, never loosened:** S1, S3a and S5 `expect.local` assert `blocked_by: xc` (agreed at the start of Phase 1), and S0 now asserts its model step (above).
- **Docs/08 rows R32–R44** were added during Phase 1 for behaviour the code depends on that only the tenant can confirm, and R24 now records the Bedrock finding.

## Open items

- `writes.actor` in the ERP is always `mcp-servers`: every ERP call comes through `svc-mcp`. Who caused a write comes from the cockpit trace and XC logs (erp-mock README says so).
- docs/03 shows no card on file for Mallory (CO-1077) and Pat (CO-1100), but the seed data gives them test cards. Harmless; pick one before the deck is final.
- The latency bench client (`measure_latency.py`) is Phase 4.
- `scripts/check_bedrock.py` (one Converse call per route from EC2 through the VPC endpoint) is still to build; the probe used here can be its starting point.

## Decide before Phase 2

- **R40: ai-gw's API key versus plain llm-lb.** ai-gw on EC2 requires `x-api-key`, but Phase 2 runs llm-lb with routing only, and llm-lb header injection comes late in Phase 3: every model call would be a 401. Either add header injection to the plain Phase 2 llm-lb (R10 is doc-confirmed), or run Phase 2 with `GATEWAY_REQUIRE_KEY=false` and flip it before Phase 3.
- **R24: Bedrock model access** (above), for the devbox role and the EC2 core role.

## What Phase 2 needs

- `scripts/render_manifests.py`: substitute `${REGISTRY}`, `${IMAGE_TAG}` and `${EXFIL_TEST_URL}` (S5 exits 2 without it), render the `agent-configs` ConfigMap, and fail on anything left unresolved.
- `scripts/build_images.sh`: multi-arch builds, each run once as `--user 1000770000:0`, then push.
- The cockpit's S5 button: create a Job from `cronjob/insights-nightly` through the vK8s API and tail its `TRACE ` lines. Until then the button says it is not built.
- `run_scenario.py --env tenant` and `smoke_test.py --env tenant` (both exit 2 today).
- Timeouts on the XC side: shop-lb and ops-lb longer than the longest agent turn (R41), cockpit-lb longer than the longest button (R44), llm-lb longer than ai-gw's 25 s Bedrock read (R39).
- `scripts/reset_demo.sh` and `scripts/preflight.sh`.
