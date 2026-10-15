# 00 Overview

## Purpose

This demo proves the solution concept from the briefing deck *vK8s as an agentic AI runtime*. vK8s runs the agent loop, and the XC fabric governs every hop. The demo should leave an internal F5 audience believing three things:

1. **XC can host agents where they belong.** Customer-facing agents run on REs close to users. High-privilege agents run on a CE beside the data. Both live in one namespace, managed from one console.
2. **XC data-path controls work as agent controls.** No new product is needed: JWT validation, service policy, rate limiting, WAF, Data Guard, API validation and malicious user detection map directly to the OWASP Top 10 for Agentic Applications.
3. **We know where the gaps are.** The demo shows the rough spots from the deck honestly: header/body trust for MCP, no claim-to-body binding, uneven egress control on REs, and no prompt-level inspection on the LB.

## Storyline

**Copperline Outfitters** is a fictional outdoor-gear retailer. It runs an ERP in AWS and has built its own AI gateway in front of Amazon Bedrock, which is what many real customers do. Copperline is rolling out agents:

| Agent | Who uses it | Where it runs | Privilege |
|---|---|---|---|
| Shopper Assistant | Customers on the website | REs (3 PoPs) | Low |
| Store Ops | Store associates | AWS CE | Medium (writes inventory) |
| Refunds Desk | Customer-care agents and supervisors | AWS CE | High (moves money) |
| Insights Agent | Marketing; a **shadow** deployment nobody reviewed | AWS CE | Should be none |
| Granite Supply agent | Copperline's supplier; a **third-party** agent | Outside Copperline (simulated by the cockpit, calling over the internet) | Partner API only |

Over the demo, the audience watches these agents work, get attacked, misbehave and get contained, while XC shows who did what, on whose behalf, and what was stopped.

## Goals

The demo should:

- Show at least eight distinct XC data-path controls acting on agent traffic, each tied to an OWASP agentic risk.
- Show agents on RE and CE in a single vK8s namespace, plus the RE-to-CE tool hop (reference architecture 3, the hero pattern).
- Keep the customer-owned AI gateway in the path, with XC governing access to it rather than replacing it.
- Produce real numbers for the deck's validation plan: RE-to-CE latency per tool call (p50/p95) and the overhead of a 30-call loop.
- Run deterministically in 20 minutes, and reset in under 60 seconds.

## Non-goals

The demo won't:

- Show F5 AI Gateway or F5 AI Guardrails. That's deliberate: the customer brought their own gateway. The close (Act 5 of the run-of-show) names them as the upsell for prompt-level inspection.
- Be production-grade. Data is fake and the IdP is a demo token service.
- Test whether a frontier model resists prompt injection. Demo-safe mode simulates a hijacked model so the beat doesn't depend on model behavior. Say so on stage.
- Use Bot Defense. It depends on client-side telemetry, and these agents are headless server-side clients. The demo says so plainly; the agent-aware Bot Defense story belongs in a separate, browser-based demo.

## Success criteria

- Every scenario in `docs/05-scenarios.md` passes three automated runs in a row against the tenant.
- A presenter who didn't build the demo can run it from `docs/06-run-of-show.md` after one rehearsal.
- The recorded backup exists before the first live showing.
