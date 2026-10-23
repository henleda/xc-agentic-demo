# Phase 1 close-out: services, locally

Date: 2026-10-23. Built and checked on the devbox (EC2 `copperline-devbox`, `deploy/devbox/`), with `make check` also run on a Mac. Everything here runs under `deploy/local/docker-compose.yaml`, which is **not XC**: the local-gateway stands in for the load balancers (docs/08 R38).

## Acceptance (docs/07, Phase 1)

| Check | Result |
|---|---|
| `make check` passes | **Pass.** 1672 passed, 36 skipped (Mac); the skips are the live-stack and Chrome tests, run separately below |
| `run_scenario.py --all --env local` passes every scenario with an `expect.local` block | **Pass**, three runs in a row (`RUNS=3 make local-scenarios`, stub model, tightened S0): 27/27 passed, `results/scenarios-20261023T185959Z.json` on the devbox. S7 has no local expectations (WAF and malicious-user detection are XC's) |
| S0 works against real Bedrock on the devbox | **Pass** (2026-10-23, after model access was fixed). `/v1/usage` `"mode": "bedrock"`; S0's model step 200 in 2.8 s; ai-gw logged `model_id: us.anthropic.claude-haiku-5-5`. See below |

Also run on the devbox: `make local-check` (35 live tests against the 18 running containers), `make local-smoke` (every hop answered by the service behind its LB), and every cockpit button through the tunnel.

### S0 against real Bedrock (docs/08 R24)

Routes: fast `us.anthropic.claude-haiku-5-5` (shopper), standard `us.anthropic.claude-sonnet-5-5` (refunds, storeops), premium `us.anthropic.claude-opus-5-5` (insights-nightly), us-east-1. The devbox has no `.env`, so the settings go on the command line:

```bash
make devbox-run CMD="LOCAL_STUB_MODEL=false ROUTE_FAST=us.anthropic.claude-haiku-5-5 \
  ROUTE_STANDARD=us.anthropic.claude-sonnet-5-5 ROUTE_PREMIUM=us.anthropic.claude-opus-5-5 make local-up"
make devbox-run CMD="uv run python scripts/run_scenario.py S0 --env local"
make devbox-run CMD="make local-up"          # back to stub mode
```

Result: S0 PASS with its model step asserted; the reply is a real answer built from the two tool results (order CO-1042 shipped, ETA 20 October, the Alpine 2 reviews). S2 and S3b also pass on Sonnet 5.5. The devbox role reached all three models twice, 150 s apart. S5 passes without reaching Bedrock: its model hop is a 403 at the gateway by design, so no scenario reaches the premium route locally (a direct Converse call to Opus 5.5 does work).

**Model access took a detour.** The first run failed: S0's model step was a 502, ai-gw logged `AccessDeniedException` ("not authorized to perform the required AWS Marketplace actions"). A model's first call in an account starts its AWS Marketplace subscription. Those first calls came from the devbox role, which has no `aws-marketplace:Subscribe`, so each model answered once and was then denied for everyone. Fix: a call to each model from the SSO `Users` role, which may subscribe, then about 5 minutes for the failed state to clear. Now `aws bedrock get-foundation-model-availability` shows `agreementAvailability: AVAILABLE` for all three. The subscription is per account, so the devbox and EC2 core roles need only `bedrock:InvokeModel`. docs/07 Phase 0 now says to make a new model's first call from the SSO role. (Updating the Mac's AWS CLI from 2.22 to 2.38 was needed for the Bedrock availability commands.)

**The failed run found a hole, now fixed.** S0 passed with the 502, because its `expect.local` asserted only step 1. It now asserts all three steps like `expect.tenant`, so a failed model hop fails S0 (`scenarios/S0-baseline.yaml`; `test_real_s0_block_fails_when_the_model_hop_fails` fails without the change). This is a tightening; no expect block was loosened.

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
- **Docs/08 rows R32–R44** were added during Phase 1 for behaviour the code depends on that only the tenant can confirm, and R24 (Bedrock models) is now verified, with how model access works.

## Open items

- **Fixed: real-model replies were cut off at 400 tokens.** On Bedrock, S0's reply (Haiku) and S3b's (Sonnet) hit `max_tokens` and ended mid-sentence (S0: "- **"). The cap was sized for the stub's short replies, and the 5.5 models think before answering, which counts against it. Now `MODEL_MAX_TOKENS = 1024` (API.md updated) and the shopper, refunds and storeops prompts ask for two or three short sentences of plain text. Re-run on Bedrock: S0, S2 and S3b all `end_turn` (434, 274 and 309 output tokens), each reply two or three sentences. S0's 434 shows the cap alone had to rise. Budgets are fine: about 1,250 tokens per call against 200k per route per day. Stub mode is unaffected (9/9 after the change).
- `writes.actor` in the ERP is always `mcp-servers`: every ERP call comes through `svc-mcp`. Who caused a write comes from the cockpit trace and XC logs (erp-mock README says so).
- docs/03 shows no card on file for Mallory (CO-1077) and Pat (CO-1100), but the seed data gives them test cards. Harmless; pick one before the deck is final.
- The latency bench client (`measure_latency.py`) is Phase 4.
- `scripts/check_bedrock.py` (one Converse call per route from EC2 through the VPC endpoint) is still to build; the probe used here can be its starting point.

## Decide before Phase 2

- **R40: ai-gw's API key versus plain llm-lb.** ai-gw on EC2 requires `x-api-key`, but Phase 2 runs llm-lb with routing only, and llm-lb header injection comes late in Phase 3: every model call would be a 401. Either add header injection to the plain Phase 2 llm-lb (R10 is doc-confirmed), or run Phase 2 with `GATEWAY_REQUIRE_KEY=false` and flip it before Phase 3.

## What Phase 2 needs

- `scripts/render_manifests.py`: substitute `${REGISTRY}`, `${IMAGE_TAG}` and `${EXFIL_TEST_URL}` (S5 exits 2 without it), render the `agent-configs` ConfigMap, and fail on anything left unresolved.
- `scripts/build_images.sh`: multi-arch builds, each run once as `--user 1000770000:0`, then push.
- The cockpit's S5 button: create a Job from `cronjob/insights-nightly` through the vK8s API and tail its `TRACE ` lines. Until then the button says it is not built.
- `run_scenario.py --env tenant` and `smoke_test.py --env tenant` (both exit 2 today).
- Timeouts on the XC side: shop-lb and ops-lb longer than the longest agent turn (R41), cockpit-lb longer than the longest button (R44), llm-lb longer than ai-gw's 25 s Bedrock read (R39).
- `scripts/reset_demo.sh` and `scripts/preflight.sh`.
