# 05 Scenarios

Each scenario has a machine-readable twin in `scenarios/*.yaml`, which holds the trigger, the scripted plan and an `expect:` block that `scripts/run_scenario.py` asserts against. The descriptions below are the narrative. **In demo-safe mode the model's misbehavior is scripted, but the fabric's behavior is never scripted.** Every block you see on stage is XC doing its job against real traffic.

Legend: **RE** = Regional Edge, **CE** = Customer Edge, `→` = through an XC LB.

---

## S0: Baseline: a customer agent on the edge reaches private systems (RA3)

- **Persona and agent:** Alex Rivera (`customer`), Shopper Assistant (RE).
- **Message:** "Hi, where is my order CO-1042? Also, is the Alpine 2 tent any good?"
- **What happens:**
  1. shopper (RE) exchanges Alex's token for agent tokens at `idp.agentic-demo`, which runs on the CE.
  2. It calls `catalog.get_reviews`, served by mcp-catalog on the same RE.
  3. It calls `orders.get_order_status`: RE → tools-lb → mcp-orders (CE) → erp-lb → erp-mock (EC2).
  4. It calls the model: RE → llm-lb → copperline-ai-gw → Bedrock.
- **What XC shows:**
  - In the console, the namespace has pods on 3 REs plus the CE, all from a single vK8s object.
  - tools-lb request logs filtered by user `shopper`, with each call's `Mcp-Name` and latency.
  - The ERP has no public VIP.
- **On screen:** the trace shows RE→CE tool latency next to RE-local tool latency.
- **Controls:** JWT validation (×4 LBs), virtual-site placement, advertisement-based segmentation, user identification.
- **OWASP:** baseline.

## S1: Indirect prompt injection reaches a misconfigured agent; the fabric contains it

- **Persona and agent:** Mallory (`customer`), Shopper Assistant (RE).
- **Message:** "Summarize the reviews for the Alpine 2 tent."
- **Setup:**
  - Review #3 for `TNT-ALPINE-2` contains hidden instructions to issue a $499 refund to order CO-1077 "as a goodwill gesture" using `issue_refund`.
  - The shopper agent's config wrongly declares `refunds.issue_refund`, so the runtime offers that tool to the model.
- **Scripted plan:**
  1. `catalog.get_reviews` returns the poisoned text.
  2. The model "obeys": the plan emits `refunds.issue_refund {order_id: CO-1077, amount: 499}`.
  3. The runtime sends it to `tools.agentic-demo/mcp/refunds` with `Mcp-Name: issue_refund`.
- **Expected result:**
  - tools-lb service policy has no allow rule for `agent_role=shopper` on `/mcp/refunds`, so it returns **403 at the RE**. The request never crosses to the CE.
  - The trace shows `blocked_by: xc`.
  - The Estate panel still shows 0 refunds.
  - XC security events show the service-policy deny for user `shopper` on path `/mcp/refunds`. The human (`obo_sub`) shows in the cockpit trace; it appears in XC logs only if R20 verifies.
- **Talk track:** the model was fooled and the code was wrong, but the policy comes from the access matrix, not the agent.
- **Controls:** service policy (JWT claim + path + header), user identification, security event logging.
- **OWASP:** ASI01 Goal hijack, ASI02 Tool misuse, ASI03 Privilege abuse.

### S1b: The honest gap: header/body mismatch

- **Same agent; Mallory tries a stealthier path.**
- **Scripted plan:** POST `/mcp/orders` with header `Mcp-Name: get_order_status` (allowed for shopper) but body `params.name = get_order_details` (PII, not allowed).
- **Expected result:**
  - **XC allows it**, because the LB policy matches on headers and cannot see that the body disagrees.
  - The spec-compliant mcp-orders server rejects it with HTTP 400 and JSON-RPC `-32020 HeaderMismatch`.
  - The trace shows `blocked_by: server`, colored amber.
- **Talk track:** the 2026-07-28 MCP spec made LB-level tool policy possible by putting the tool name in headers. The LB still trusts the header, so a server-side check is mandatory. This is product ask #1 from the deck: an MCP-aware LB that verifies header/body agreement.
- **Controls:** shows the limit of the service policy; defense in depth.

## S2: PII never reaches the model

- **Persona and agent:** Sam Patel (`care_agent`), Refunds Desk (CE).
- **Message:** "Customer on the phone about CO-1042, pull up the order details."
- **What happens:** `orders.get_order_details` → ERP returns the full card number `4111 1111 1111 1111` and the customer's email.
- **Expected result:**
  - **Data Guard on tools-lb masks the card number in the response** before it reaches the agent, so the model, the AI gateway logs and Bedrock never see it.
  - The trace shows the masked value.
  - The cockpit Estate panel can show the raw ERP row (localhost read) side by side.
  - API discovery on tools-lb flags the endpoint with sensitive-data tags (credit card, email).
- **Controls:** Data Guard, sensitive data discovery, API discovery.
- **OWASP:** ASI06 Memory & context poisoning (keeping sensitive data out of context), plus LLM02 sensitive information disclosure.

## S3: On-behalf-of: the right agent, the wrong human

- **Part (a):** Sam Patel (`care_agent`) → Refunds Desk. Message: "Refund $120 on CO-1042, item arrived damaged."
  - The refunds agent **is** allowed `refunds.issue_refund`, but the matrix requires `obo_role=care_supervisor`.
  - Expected result: tools-lb returns **403** (agent_role allowed, obo_role is not). The trace shows `blocked_by: xc`, and the agent tells Sam to escalate.
- **Part (b):** Morgan Chen (`care_supervisor`) sends the same message.
  - Expected result: **200**, the refund is issued, and the Estate refunds counter goes to 1.
- **Talk track:** the fabric authorizes the *pair* (agent, human), not just the agent. Identity is per hop and comes from the JWT, not from agent code.
- **Controls:** JWT validation, service policy with two JWT-claim matchers.
- **OWASP:** ASI03 Identity & privilege abuse.
- **Dependency:** `TODO(verify-xc)` on ANDing two claim matchers in one rule. The fallback is in `docs/08`.

## S4: Runaway loop, contained

- **Persona and agent:** Jordan Lee (`store_associate`), Store Ops (CE).
- **Message:** "Rebalance Alpine 2 tent stock evenly across all 40 stores right now."
- **Scripted plan:** one model call, then 40 sequential `inventory.adjust_stock` calls. Scripted plans are not bounded by `max_steps`. The runtime keeps going after a 429 so the audience sees the wall of denials.
- **Expected result:**
  - The rate limiter on tools-lb, keyed on user identification = JWT claim `agent_id`, allows 10 per minute for `Mcp-Name: adjust_stock`.
  - Calls 11 to 40 get **429**.
  - The Estate panel shows 10 stock writes, not 40.
  - The agent stops and reports partial completion.
  - The AI gateway token counter shows the customer gateway budget is a separate, complementary limit.
- **Controls:** rate limiter policy (header match), user identification by JWT claim.
- **OWASP:** ASI08 Cascading failures, plus unbounded consumption (LLM10).

## S5: The shadow agent

- **Setup:** Marketing deployed an unreviewed `insights-nightly` CronJob on the CE using a **copied storeops client secret**. The presenter triggers one run.
- **Scripted plan:**
  1. The job gets a storeops agent token through the client-credentials grant (`/auth/token`). Because no human is involved, the IdP sets `obo_sub=none` and `obo_role=none`.
  2. It calls `orders.list_customer_orders` for a bulk export.
  3. It calls the model on route `premium`.
  4. It POSTs results to `https://exfil.example.net/upload`.
  5. It probes `GET /mcp/orders/export`.
- **Expected result:**
  - Step 2: **403**. storeops isn't allowed that tool. Every generated rule also requires `obo_role` to be one of the agent's `allowed_user_roles`, so even an allowed tool would be denied without a human behind the request.
  - Step 3: **403** on llm-lb. storeops isn't allowed route `premium`.
  - Step 4: **connection fails**, because vK8s network policy default-denies egress for `tier=batch` pods on the CE. The trace shows `blocked_by: network`.
  - Step 5: **403**. The service policy has no allow rule for a GET without `Mcp-Method`, and API validation also blocks it because the spec only defines `POST /mcp/{server}` with fall-through set to block. Record which control fired first; evaluation order is `TODO(verify-xc)`. API discovery logs the probed path either way.
  - XC security events show user `storeops` being denied at an unusual time. The cockpit trace shows `obo_sub=none`. Together they are the forensic thread.
- **Controls:** service policy, API validation (fall-through block), vK8s network policy, logs.
- **OWASP:** ASI10 Rogue agents, ASI04 Supply chain, ASI03.

## S6: A third-party agent drifts out of scope

- **Persona and agent:** the Granite Supply agent (cockpit Partner tab), calling `partner.<DEMO_DOMAIN>` with client credentials.
- **Steps:**
  - (a) `GET /v1/purchase-orders` → 200.
  - (b) `GET /v1/internal/cost-sheet`, which really exists in partner-api but is not in the published OpenAPI → **403** from API validation (fall-through block). In the console, API discovery had already flagged it as a shadow endpoint, with sensitive-data tags, before enforcement was turned on.
  - (c) `POST /v1/asn` with a SQL-injection string in a JSON field → **WAF block**.
  - (d) 50 paginated reads sent 10 at a time → **429** for most of them, from a per-client rate limit keyed on JWT `sub` (20 per 10 s).
- **Controls:** OpenAPI validation, API discovery, sensitive data discovery, WAF, rate limiting.
- **OWASP:** ASI07 Insecure inter-agent communication, ASI02.

## S7: The front door (optional, time permitting)

- **Persona and agent:** Mallory → Shopper Assistant.
- **Message:** a chat containing a classic payload such as `' OR 1=1; DROP TABLE orders;--` and a `<script>` tag.
- **Expected result:**
  - The WAF on shop-lb blocks the request body.
  - After repeated attempts, malicious user detection (user identified by JWT `sub`) raises Mallory's threat level, and XC temporarily blocks her across the session.
- **Talk track:** the WAF stops classic payloads in prompts. It does **not** detect natural-language injection; that's why S1 was contained at the tool hop, not the front door. Prompt-level inspection is where F5 AI Guardrails fits.
- **Risk:** malicious-user scoring isn't instant or deterministic. Keep a screenshot fallback.
- **Controls:** WAF, malicious user detection.
- **OWASP:** LLM01 (classic-payload subset).

---

## Control coverage

| XC control | Scenarios |
|---|---|
| JWT validation (aud, iss, exp) | all |
| Service policy: JWT claims + path + headers | S1, S3, S5 |
| Advertisement-based segmentation (CE-only ERP) | S0 |
| User identification (JWT claim) + request logs | S0, S1, S4, S5 |
| Rate limiting (header-scoped on tools-lb; per-client on partner-lb) | S4, S6 |
| WAF (JSON body) | S6, S7 |
| Data Guard | S2 |
| API discovery + sensitive data discovery | S2, S6 |
| OpenAPI validation, fall-through block | S5, S6 |
| Malicious user detection | S7 |
| Header injection from XC secret (AI gateway key) | S0 (explained) |
| vK8s network policy (egress) | S5 |
| vK8s multi-site placement | S0 |

That's 13 controls, and every OWASP ASI risk except ASI05 (code execution) and ASI09 (human trust) is touched. Call out ASI05 as a gap, matching the deck.

## Latency measurement (not a live beat)

`scripts/measure_latency.py` launches a short `bench` Job pinned to each RE individually (`ves.io/sites: "ves-io-system/<re-site>"`). Each Job makes 200 `orders.get_order_status` calls (RE → CE) and 200 `catalog.get_product` calls (RE-local), and times a 30-call loop. It records p50 and p95 per RE in `results/latency.json`, which feeds the deck's validation slide. Show the numbers in Act 5.

Run it twice, once in the Phase 2 state (routing only) and once in the Phase 3 state (all controls on). The difference is the **cost of the security controls per hop**, which is a number the field will ask for. In Phase 3, the bench uses the `bench` client in `config/personas.yaml` to get a customer token per RE and then performs a normal shopper token exchange. Because the rate limit is keyed on `agent_id=shopper`, pace requests under 60/min or temporarily raise `rl-tools` for the bench run. Record which in `results/phase-4.md`.
