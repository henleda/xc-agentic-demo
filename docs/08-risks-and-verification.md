# 08 Risks and verification log

Every XC behavior the demo depends on is listed here. The **Status** column starts from desk research done in October 2026, using public F5 docs as reported in search results, not by reading pages directly. Claude Code updates this table as it verifies each item against the tenant.

Status values: `doc-confirmed` (public docs say so), `doc-partial`, `unverified`, `tenant-verified`, `tenant-failed`.

| ID | Assumption | Status | Scenario impact | Fallback |
|---|---|---|---|---|
| R1 | An AWS VPC site in **App Stack** mode runs vK8s; m5.2xlarge minimum | doc-confirmed | All | **Ask F5 PM first.** AWS VPC sites are slated for deprecation in favor of Secure Mesh Site v2, and App Stack on SMSv2 appears to be Early Access. If SMSv2 + App Stack is available to the F5 tenant, prefer it and note the change |
| R2 | One vK8s object spans an RE virtual site and a CE virtual site; Deployments are pinned with `ves.io/virtual-sites: "agentic-demo/vs-ce-aws"` | doc-confirmed | All | Use `ves.io/sites` with explicit site names |
| R3 | RE pods can reach an internal LB advertised on "vK8s Service Network on RE" whose origin is a CE vK8s service | doc-confirmed (xchaawsdemoguide "re2ce" pattern) | S0, S1 | None needed; this is the documented pattern |
| R4 | CE pods can reach an internal LB advertised on the CE's own vK8s service network | doc-partial | S2–S5 | Advertise on the CE **site-local inside** network and call the VIP by IP or DNS. Document the change |
| R5 | An LB **not** advertised on REs is unreachable from RE pods (ERP segmentation) | unverified | S0 talk track | If it is reachable, add a service policy on erp-lb that allows only `svc_role=tool-server` |
| R6 | A service policy rule can AND a JWT-claim matcher, header matchers and a path matcher | doc-partial | S1, S3, S5 | Split into separate LBs or routes per tool server. If two JWT-claim matchers can't be ANDed (S3), have the IdP mint a composite claim `authz_pair = "<agent_role>:<obo_role>"` and match on that one claim |
| R7 | JWT-claim matching works only when JWT validation is enabled on the same LB | doc-confirmed | All | Already designed in |
| R8 | Service policies attach per LB, **not** per route | doc-confirmed | Design | Path matchers inside one LB-level policy (already the design) |
| R9 | XC cannot forward JWT claims as headers to the origin | doc-partial (not found) | S0 (llm-lb header logging) | The agent sends `x-agent-id` / `x-obo-sub`. sp-llm requires the header to equal the claim, **if** a rule can compare a header with a claim (unverified). Otherwise the gateway decodes the JWT itself (it is forwarded unchanged) |
| R10 | `request_headers_to_add` can take its value from a blindfolded secret (AI gateway key) | doc-confirmed | S0 | Store the key as a plain header value and say so on stage |
| R11 | Rate limiter policy can match on a header (`Mcp-Name`) and count per user identification on a JWT claim | doc-confirmed | S4 | Use API-protection endpoint rate limits on path. Give `adjust_stock` its own path, `/mcp/inventory/adjust`, in safe mode |
| R12 | Data Guard masks Luhn-valid card numbers in **JSON** responses; needs WAF enabled | doc-confirmed | S2 | Sensitive Data Exposure Rules under API protection |
| R13 | API discovery shows shadow endpoints and sensitive-data tags within hours | doc-partial | S6 | Pre-seed traffic 24 h before; keep a screenshot |
| R14 | OpenAPI validation with fall-through **Block** rejects undocumented paths | doc-confirmed | S5, S6 | Service policy deny for `/v1/internal/*` (weaker story) |
| R15 | WAF blocks SQLi/XSS inside JSON request bodies | doc-partial | S6, S7 | Put the payload in a query string |
| R16 | Malicious user detection escalates to a block for a headless client identified by JWT `sub`; challenges are useless for API agents | doc-partial | S7 | Keep S7 optional; use a screenshot |
| R17 | Malicious-user state can be reset between runs | unverified | S7 | Rotate Mallory's `sub` per run |
| R18 | vK8s network policy can deny egress for labelled pods on the CE | doc-confirmed (L3/L4) | S5 | CE forward proxy policy with a TLS domain allow-list. **Enhanced firewall policies do not apply to vK8s** |
| R19 | No domain-based egress control for vK8s pods on REs | doc-confirmed | Talk track | This is a deck gap (product ask #3); show it, don't hide it |
| R20 | Custom headers (`Mcp-Name`, `x-agent-id`) appear in request logs or can be filtered on | unverified | S0, S1 | User identification on the JWT claim makes the agent searchable as "user". Tool-level detail comes from the cockpit trace and server logs |
| R21 | XC API discovery tags MCP server endpoints | doc-partial (changelog, undated) | Bonus | Mention only if seen in the tenant |
| R22 | XC WAF has no natural-language prompt-injection detection | doc-confirmed | S1, S7 framing | Designed in; this is the AI Guardrails upsell |
| R23 | Bot Defense agent-aware policy needs client telemetry and is unsuitable for server-side agents | doc-partial | Not used | Not in the demo. An optional shop UI beat could show AI-crawler categories |
| R24 | Bedrock models chosen for `fast`/`standard`/`premium` are enabled in the region with Converse + tool use | unverified | All | `DEMO_STUB_MODEL=true` with canned replies |
| R25 | Jobs and CronJobs are supported in vK8s; `suspend: true` CronJob plus a manual Job create works | doc-confirmed (CronJob listed) | S5 | A Deployment with `replicas: 0` that the cockpit scales to 1 |

## XC block response samples

Fill these in during Phase 3. The XC block detector in `services/common` depends on them.

| Control | Status | Content-Type | Body signature |
|---|---|---|---|
| Service policy deny | | | |
| JWT validation fail | | | |
| WAF block | | | |
| Rate limit | | | |
| API validation block | | | |
| Malicious user block | | | |

## Open questions for F5 PM / XC engineering

1. Is the recommended CE for vK8s in AWS today an AWS VPC site in App Stack mode or SMSv2 (R1)?
2. Can a service policy rule match a header value against a JWT claim value (R9)?
3. Is there a supported way to reset malicious-user state per user (R17)?
4. Are there limits on rules per service policy that matter at around 30 rules?

## Added during spec review

| ID | Assumption | Status | Scenario impact | Fallback |
|---|---|---|---|---|
| R26 | An origin pool on an RE virtual site (`op-mcp-catalog`, `op-shopper`) serves each request from the **local** RE pod, not a remote RE | unverified | S0 latency story | Measure in `measure_latency.py`; if remote, pin catalog per RE or accept and explain |
| R27 | Jobs can be pinned to a single RE with `ves.io/sites` and run once on that site | unverified | Latency bench | Use a Deployment per RE, scaled 1 → 0 after the run |
| R28 | Rate limits keyed on `agent_id` are shared by every replica and every user of that agent (e.g. all shoppers) | by design | S4 | Fine for the demo. In production, key on `agent_id` + `obo_sub` (composite claim) |
| R29 | OpenAPI validation on partner-lb can be scoped to `/v1/*`, so `/auth/token` (not in the spec) is not hit by fall-through block | unverified | S6 token step | Give the token endpoint its own LB (`partner-auth-lb`) with no API definition |
| R30 | App Stack AWS VPC site supports two interfaces (outside and inside) | unverified | All EC2 origins | Use one interface, set IP origin pools to the outside network, and allow the ce-outside subnet in `sg-core` |
| R31 | `${EXFIL_TEST_URL}` is reachable from a CE vK8s pod **without** the network policy | to check in Phase 2 | S5 | Proves the policy (not DNS or NAT) is what blocks exfil |

## Added during Phase 1 (local build)

| ID | Assumption | Status | Scenario impact | Fallback |
|---|---|---|---|---|
| R32 | XC passes the origin response header `x-copperline-origin` through unchanged, and XC-generated block responses (service policy, WAF, rate limit, JWT, API validation) do **not** carry it. `xcblock` uses this to tell `xc` from `server` | unverified | S1, S1b, S3a, S4, S5, S6: every `blocked_by` assertion | Match on the XC block-page or JSON signature recorded in the table above |
| R33 | XC does not rewrite or drop a client-supplied `x-request-id` on internal and public LBs | unverified | Log correlation only | Correlate by timestamp and `agent_id`; the cockpit trace carries its own request ID per step |
| R34 | The custom request header `x-demo-scenario` reaches the origin through llm-lb unchanged (the AI gateway's stub mode keys canned replies on it) | unverified | Only `DEMO_STUB_MODEL=true` (outage fallback) | The stub falls back to a generic reply. Not on stage, since real Bedrock is the default |
| R35 | XC JWT validation accepts the literal string `"none"` for the mandatory claims `obo_role` / `obo_sub` (machine-mode tokens), so S5 is denied by **service policy** (403), not by JWT validation (401) | unverified | S5 i=2, i=3 | If XC rejects it, S5 still shows a fabric block (401). Update the `expect.tenant` status with the user's approval; do not silently loosen it |
| R36 | XC applies a load balancer's JWT validation **before** its service policy, so a request with no valid token is a 401 even on a path the policy would deny. The local-gateway assumes this order | unverified | S5 (machine tokens), any 401-vs-403 assertion | Record the real order in Phase 3 and update `deploy/local/local-gateway` to match |
| R37 | A service-policy header matcher (`Mcp-Name`, `Mcp-Method`, `x-model-route`) sees a header that the client sent **twice**. The local-gateway treats a repeated header as never matching (deny). Behind XC, the origin is the backstop: the MCP servers reject a repeated `Mcp-*` header with -32020 (check 1a), and ai-gw rejects a repeated `x-model-route` with 400 (docs/03) | unverified | S1b variants (tools-lb); llm-lb route matching (the S5 i=3 shape) | Both origins reject repeats, so neither can be tricked into acting on a copy XC did not judge. Record XC's behavior |
| R38 | The local-gateway is a stand-in, not a model of XC. It emulates routes, JWT validation, service policy and header changes from the intent files, and nothing else (no WAF, Data Guard, rate limits, API validation, user identification, malicious users). A local pass says the services and the policy *intent* agree, not that XC does | by design | Every `expect.local` | Only `expect.tenant` counts as evidence. See `deploy/local/README.md` |
| R39 | llm-lb's route/idle timeout is longer than ai-gw's Bedrock read timeout (25 s), so ai-gw's own 504 `bedrock_timeout` reaches the agent. One review cited a 30 s XC default (F5 KB K000146855); not checked here | unverified | Any slow model call; S0–S4 model steps on stage | Set the llm-lb route timeout explicitly in Phase 3 and record it; keep ai-gw's read timeout below it and the agent's model-call timeout above it |
| R40 | Phase 2 runs llm-lb with routing only, but ai-gw on EC2 requires `x-api-key` (`GATEWAY_REQUIRE_KEY: "true"` in `deploy/aws/core/docker-compose.yaml`), and llm-lb header injection is only added late in Phase 3. Every model call would then be a 401 from ai-gw | to decide before Phase 2 | Phase 2 smoke test and S0 | Either add llm-lb header injection to the plain Phase 2 LB (R10 is doc-confirmed), or run Phase 2 with `GATEWAY_REQUIRE_KEY=false` and flip it before Phase 3. Raised by the step-6 review |
| R41 | shop-lb and ops-lb wait longer than the longest agent turn, so the cockpit always gets the agent's `{reply, trace}`. A turn is one request through the front door: token exchanges, the plan's tools, and model calls of up to 25 s each (ai-gw's read timeout; up to `max_steps` = 8 in the normal loop). If the front door gives up first, the cockpit gets a bare XC 504 with no trace. A 30 s XC default route timeout was cited in the step-6 review (unverified) | unverified | Every agent scenario on real Bedrock, mostly the unscripted loop | Set the shop-lb/ops-lb route timeout explicitly in Phase 3 (TODO(verify-xc): field name) above the worst turn; locally the front-door gateways wait 600 s |
| R42 | XC sensitive-data discovery tags `/v1/internal/cost-sheet` on partner-lb as sensitive from its response body (an IBAN, an email, a phone number) with the default detectors | unverified | S6(b) "discovery flagged it with sensitive-data tags" | Add a custom sensitive data type (IBAN regex) in Phase 3 and record it in `deploy/xc/MANUAL_STEPS.md`; the email and phone may be enough on their own |
| R43 | An endpoint API discovery found in Report mode stays in the partner-lb API inventory after OpenAPI validation (fall-through block) stops every request to it, and is not purged as inactive before the show | unverified | S6(b) talk track: "discovered before enforcement" | Run the discovery seed (`MANUAL_STEPS.md`) shortly before each show, or screenshot the inventory as backup |
