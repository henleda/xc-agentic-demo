# Local mode (laptop, no XC)

`make local-up` starts every service with docker compose, plus `local-gateway`, a small Envoy or Caddy config that serves the internal LB hostnames (`tools.agentic-demo`, `llm.agentic-demo`, `idp.agentic-demo` and `erp.agentic-demo`) as compose network aliases. It does rough JWT and path checks.

**It does not emulate XC.** There is no WAF, Data Guard, API validation, malicious user detection or claim-level fidelity.

Scenario files carry separate, looser `expect.local` blocks for this mode. Use local mode to develop agent, tool and IdP logic quickly. Never use it to demonstrate anything.

Claude Code: write `docker-compose.yaml` and `local-gateway/` here in Phase 1.
