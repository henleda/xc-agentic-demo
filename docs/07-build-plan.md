# 07 Build plan

The plan has five phases. Each phase ends with an acceptance check that Claude Code runs and records in `results/phase-<n>.md`. Estimates assume one builder working with Claude Code, plus one person who has XC tenant and F5 AWS account access.

## Phase 0: Environment (about 1 day, human-led)

Most of this phase needs a human with access. Claude Code writes the Terraform and checklists.

1. In the F5 AWS account, create VPC `10.50.0.0/16` with subnets: CE outside (public), CE inside (private), and core (private). Add a NAT gateway for the `core` subnet (the cockpit on EC2 needs outbound internet to reach the public XC LBs and the vK8s API, and the host pulls images). The CE outside subnet uses the internet gateway.
2. Create the `bedrock-runtime` interface VPC endpoint in the core subnet. Enable model access in Bedrock for the three route models (`fast`, `standard`, `premium`). Use inference profile IDs where the region requires them. Model access means the account's AWS Marketplace subscription to each model, started by the model's first call. Make that first call from a role with `aws-marketplace:Subscribe` (the SSO `Users` role), never from a service role: a caller without it gets one answer, then `AccessDeniedException` for everyone (docs/08 R24). The three route models are already subscribed in this account.
3. Create EC2 `copperline-core` (t3.large, Amazon Linux 2023, Docker). Give it an instance role with `bedrock:InvokeModel` (Converse is authorized by it; there is no `bedrock:Converse` action) on the three route inference profiles (`arn:aws:bedrock:us-east-1:<account>:inference-profile/us.anthropic.claude-*-5-5`) **and** their foundation models in every Region the profiles route to: us-east-1, us-east-2, us-west-2 (`aws bedrock get-inference-profile` lists them). A `us.` profile call is checked against both; with only one, every call is `AccessDeniedException`. `deploy/devbox/main.tf` has a working policy. Its security group allows 8081, 8090 and 8095 from the CE inside subnet only.
4. In XC:
   1. Create the namespace `agentic-demo`.
   2. Create the AWS cloud credential.
   3. Create the AWS VPC site `copperline-aws-ce` in App Stack mode, size m5.2xlarge, using the existing VPC and subnets.
   4. Apply the site label.
   5. Create both virtual sites and the vK8s object.
   6. Download the vK8s kubeconfig to `~/.kube/agentic-demo.yaml`. Never commit it.
5. Delegate `<DEMO_DOMAIN>` to XC.
6. Create a container registry that the vK8s namespace can pull from: ECR or the team registry. Add an image pull secret.

**Acceptance check:**

- The CE site is ONLINE.
- `kubectl --kubeconfig ~/.kube/agentic-demo.yaml get pods` works.
- A test `nginx-unprivileged` Deployment pinned to each virtual site runs.
- Bedrock Converse works from EC2 through the VPC endpoint (`scripts/check_bedrock.py`).

## Phase 1: Services, locally (about 3–4 days, Claude Code)

1. `services/common`: JSON logging, request IDs, JWT helpers (verify RS256 against JWKS), the MCP 2026-07-28 client and server helpers, and the XC block detector.
2. `idp`, `erp-mock` (with seed data), `ai-gw` (with `DEMO_STUB_MODEL`), the `mcp-servers` image (4 servers), `agent-runtime` (model loop, tool loop, safe mode, trace), `partner-api`, `cockpit`.
3. `scripts/gen_keys.py`, `scripts/gen_policy.py`, `scripts/render_secrets.py`.
4. `deploy/local/docker-compose.yaml`: run every service, plus `local-gateway` (Python, `deploy/local/local-gateway/`): one container per XC load balancer except `cockpit-lb`, reading `deploy/xc/intent/` for routes, JWT validation, service policy and header changes. It keeps the internal LB names. This gives a rough stand-in for XC, so scenario logic can be developed offline. It must be clearly labeled as **not** representing XC behavior. See `deploy/local/README.md`.
5. Unit tests. Every MCP server must reject a header/body mismatch. The IdP must refuse exchanges for disallowed user roles. `gen_policy` must flag `OVERDECLARED` and missing tools.

**Acceptance check:**

- `make check` passes.
- With `make local-up` running, `scripts/run_scenario.py --all --env local` passes every scenario that has an `expect.local` block.
- S0 works against real Bedrock: on the devbox (its instance role holds the credentials), with `LOCAL_STUB_MODEL=false` and `ROUTE_*` set (see `deploy/local/README.md`). `GET 127.0.0.1:8190/v1/usage` must report `"mode": "bedrock"` and S0's model step a real `model_id`, so a stub reply can never pass for it.

## Phase 2: Deploy to vK8s with plain LBs (about 2 days)

1. Build multi-arch images. Verify they run as an arbitrary UID: `docker run --user 1000770000:0`. Push them.
2. Apply `deploy/vk8s/*.yaml`, with secrets rendered.
3. Run compose on EC2 `copperline-core` (`deploy/aws/core/docker-compose.yaml`), with `GATEWAY_REQUIRE_KEY=false` added to its `.env` (the template ships `true`): the plain llm-lb does not add ai-gw's `x-api-key` yet, so with the check on every model call would be a 401 (docs/08 R40).
4. Create all origin pools and LBs with **only** routing and advertisement configured. No JWT, policies, WAF or rate limits yet.
5. Run the advertisement check from `docs/04`.

**Acceptance check:**

- `scripts/smoke_test.py --env tenant --no-security` passes.
- S0 returns a correct answer.
- `erp.agentic-demo` cannot be reached from an RE pod.

## Phase 3: Turn on XC controls one at a time (about 3 days)

Order: JWT validation → user identification → `sp-tools` and `sp-llm` (generated) → `sp-ops`, `sp-idp`, `erp-lb` rules → rate limiters → WAF → Data Guard → API definitions (Report first, then Block) → malicious user detection → llm-lb header injection → vK8s network policy.

At the llm-lb header injection step, turn ai-gw's key check back on (docs/08 R10, R40):

1. In EC2's `.env`, check that `GATEWAY_API_KEY` is set and equals the value blindfolded into `aigw-api-key`. With the check on, ai-gw will not start without it.
2. Set `GATEWAY_REQUIRE_KEY=true` (or delete the line), then **recreate** ai-gw: `docker compose up -d copperline-ai-gw` with the same compose file and `.env` as before. `docker compose restart` keeps the old environment.
3. Confirm the check is on before testing through XC. On the EC2 host, `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8090/v1/converse` must print `401` (`400` means the check is still off). ai-gw's `aigw_ready` log line also shows `require_key`.
4. Re-run S0. A 401 from ai-gw now means llm-lb is not injecting the key.

After each step, re-run the smoke test and every scenario that touches that control. Record the actual XC block response (status code, headers, body) in `docs/08`, and update the XC block detector in `services/common`.

**Acceptance check:** `scripts/run_scenario.py --all --env tenant` passes every scenario's `expect.tenant` block.

## Phase 4: Hardening and show readiness (about 2 days)

1. Run every scenario 3 times in a row. Fix flakiness by adjusting rate-limit windows or adding resets. Do not loosen expectations.
2. Write `scripts/reset_demo.sh`:
   - ERP reset
   - AI gateway usage reset
   - Rotate `RUN_SALT`, which the cockpit passes to `/auth/demo-login` so Mallory gets a fresh `sub` per run (`mallory.x-<salt>`). `TODO(verify-xc)`: can malicious-user state be cleared without waiting out the 20-minute cooling-off period?
   - Rate-limit windows (60 s) are not cleared by the reset. `preflight.sh` waits them out before a live show; the runner orders S4 and S6 last (see `scenarios/README.md`).
3. Run `scripts/measure_latency.py` and write the results to `results/latency.json`.
4. Record the backup video, one clip per scenario. Store it outside the repo.
5. Run a dry rehearsal with someone who didn't build the demo. Fix the docs where they stumble.

**Definition of done:** see `CLAUDE.md`.

## Rough total

About 11–12 working days, plus reserve for XC surprises. Phase 3 carries the most schedule risk.
