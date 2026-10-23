# 06 Run of show (20 minutes)

**Screens:**

- **Left:** cockpit, with chat and trace on the left side and the Estate panel on the right.
- **Right:** XC console tabs, pre-opened in this order:
  1. vK8s workloads
  2. tools-lb security events
  3. tools-lb request logs
  4. API inventory (partner-lb)
  5. llm-lb request logs

**Before going on stage (T-15 min):**

1. Run `scripts/reset_demo.sh`.
2. Run `scripts/preflight.sh`. It runs the smoke test plus `--dry` for every scenario.
3. Confirm that the recorded backup video is open in a paused player.
4. Confirm `DEMO_SAFE_MODE=true`.

| Time | Act | Beat | Do | Say (key line) | Fallback |
|---|---|---|---|---|---|
| 0:00 | **1. Setup** | Frame | Show the architecture slide | "Copperline is rolling out five agents. Some are customer-facing on the edge, some are high-privilege next to the ERP, one is a third party's, and one nobody approved." | n/a |
| 1:30 | | Placement | XC console: vK8s namespace. Pods on 3 REs plus the CE | "One namespace and one manifest. Placement comes from a virtual-site label. Customer-facing agents run in the PoPs; anything that can move money runs beside the data." | Screenshot |
| 3:00 | **2. Normal day** | S0 | Cockpit: Alex asks about CO-1042 | "An edge agent just reached a private ERP through the fabric. There was no VPN and no inbound firewall hole. Every hop shows up as an LB log line tagged with the agent's identity, and the trace shows which human it acted for." | Recorded clip S0 |
| 5:00 | | Customer gateway | Point at the Estate panel token counter; open llm-lb logs | "This is Copperline's own AI gateway. We didn't replace it. XC decides which agent can reach which model route, and injects the gateway key so no agent ever holds it." | n/a |
| 6:00 | **3. Attacks** | S1 | Mallory: "Summarize the reviews…" | "The review is poisoned, the model takes the bait, and a developer accidentally gave this agent the refund tool. Watch the trace. **403 at the edge.** The request never left the RE, and the refund counter stays at zero." | Clip S1 |
| 8:00 | | S1b | Click **S1b** (the "stealth" call) | "Here's the honest part. Mallory puts an allowed tool name in the header and a forbidden one in the body. **XC lets it through**, because the LB trusts the header. The MCP server catches the mismatch. That's our product ask: an MCP-aware LB." | Clip |
| 9:30 | | S2 | Sam pulls CO-1042 details | "The ERP returned a full card number. The agent, the gateway and Bedrock only ever saw the masked value. API discovery tagged the endpoint as carrying card data on its own." | Clip |
| 11:00 | | S3 a/b | Sam asks for a refund (403), then Morgan does (200) | "Same agent, same tool, different human. The fabric authorizes the pair. Identity travels per hop in the token, not in agent code." | Clip |
| 13:00 | **4. Agents misbehaving** | S4 | Jordan: "Rebalance across all 40 stores" | "Runaway loop. Ten writes land, then 429s. That limit lives in the fabric, separate from Copperline's token budget. Two different controls for two different owners." | Clip |
| 14:30 | | S5 | Click **S5** (runs insights-nightly) | "Nobody reviewed this agent and it's using a stolen secret. Bulk PII: 403. Premium model: 403. Exfil: no route out. Probing endpoints: 403. And here's the forensic trail: a known agent identity being denied in the middle of the night, with no human behind it." | Clip |
| 16:30 | | S6 | Click **S6** (the cockpit plays Granite Supply): rows a, b, c, then d as one row of 50 reads | "Granite Supply's agent isn't ours, but our API is. Discovery found the internal cost-sheet endpoint before we enforced anything. Now it's blocked, along with the injection and the scraping." | Clip |
| 18:00 | | S7 (optional) | Mallory sends SQLi in chat | "The WAF stops classic payloads in prompts. It doesn't understand natural-language injection; that's why S1 was stopped at the tool hop. Prompt inspection is where AI Guardrails fits." | Skip if behind |
| 18:30 | **5. Close** | Numbers and gaps | Show the `results/latency.json` slide and the gap list | "The RE-to-CE tool hop costs *N* ms at p95. What worked: 13 controls, no new products. What's missing: header/body binding for MCP, agent workload identity, egress control on REs, and sandboxed code execution." | n/a |
| 20:00 | | End | | | |

## Presenter rules

- Never type a live prompt that isn't on a scenario button. Free-form prompts break safe-mode determinism.
- If the trace shows a different outcome than expected, say "let's look at what the fabric saw" and switch to the console. Logs still make the point. If they don't, go straight to the recorded clip.
- Say this once, early: "The model's bad behavior is scripted so the demo is repeatable. Everything the fabric does is live."
