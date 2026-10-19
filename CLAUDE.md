# CLAUDE.md — Copperline agentic demo

This repo specifies, and will contain, a live demo of **F5 Distributed Cloud (XC) vK8s as an agentic AI runtime**. Several AI agents for a fictional retailer, Copperline Outfitters, run in one vK8s namespace that spans F5 Regional Edges (RE) and a Customer Edge (CE) in AWS. Every hop an agent takes goes through an XC HTTP load balancer: user to agent, agent to tool, agent to model. That way XC data-path controls (JWT validation, service policy, rate limiting, WAF, Data Guard, API discovery and validation, malicious user detection) visibly govern agent behavior.

Right now the repo is a **spec plus skeletons**. Your job is to build it out.

## Read these first, in order

1. `docs/00-overview.md`: goals, audience, non-goals
2. `docs/01-architecture.md`: topology, naming, traffic flows
3. `docs/02-agents.md`: agent matrix and identity model
4. `docs/03-services.md`: interface contracts for every service
5. `docs/04-xc-configuration.md`: every XC object and the demo beat it powers
6. `docs/05-scenarios.md`: scenario definitions and expected outcomes
7. `docs/06-run-of-show.md`: the 20-minute live script
8. `docs/07-build-plan.md`: phases, acceptance criteria, definition of done
9. `docs/08-risks-and-verification.md`: what is verified, what is not, and the fallbacks

## Golden rules

1. **One source of truth for access.** `config/policy/tool-access-matrix.yaml` defines which agent role may call which tool. Never hand-edit derived artifacts. `scripts/gen_policy.py` renders the XC service-policy intent (`deploy/xc/intent/service-policies.generated.yaml`) and checks the agent configs against it. Run `make check` before committing.
2. **Do not invent XC fields.** Before writing any `volterra_*` Terraform resource, inspect the real schema with `terraform providers schema -json | jq '.provider_schemas[].resource_schemas["volterra_http_loadbalancer"]'` (or the equivalent for that resource). If a field from `deploy/xc/intent/*.yaml` has no clear mapping, leave `# TODO(verify-xc): <question>` in the code, add a row to the table in `docs/08-risks-and-verification.md`, and move on. Console-only steps go in `deploy/xc/MANUAL_STEPS.md`.
3. **vK8s workload constraints are hard requirements.** No root: every Dockerfile sets a numeric `USER` (e.g. `USER 10001`) and makes app dirs group-0 writable. Images must run as an arbitrary UID/GID. Mount an `emptyDir` at `/tmp` because the root filesystem is read-only. No privileged pods, no host networking, PID or IPC. Container ports must be above 1024 (use 8080). Every Deployment carries the `ves.io/virtual-sites` annotation so it lands on exactly one virtual site. vK8s is "Kubernetes-compatible", not conformant, so stick to Deployment, Service, ConfigMap, Secret, Job and CronJob.
4. **Service conventions.** Python 3.12, FastAPI and uvicorn, managed with `uv`. Listen on `0.0.0.0:8080` in vK8s; on EC2, erp-mock uses 8081 (admin 8181), ai-gw 8090 (admin 8190) and cockpit 8095. Every service exposes `GET /healthz`. Write JSON logs to stdout with `ts`, `service`, `request_id`, `agent_id`, `obo_sub`, `event`, `status` and `latency_ms`. Propagate `x-request-id` on every outbound call, and generate one if it is missing. Use shared code from `services/common/`.
5. **Tool servers return `application/json`, never SSE.** XC WAF and Data Guard have to see whole response bodies, and the MCP 2026-07-28 spec allows plain JSON responses.
6. **MCP version.** Implement MCP **2026-07-28** stateless Streamable HTTP. Clients must send the `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers. Servers must reject a header/body mismatch with HTTP 400 and JSON-RPC error `-32020`. First check whether the official MCP Python SDK supports 2026-07-28. If it doesn't, use the minimal implementation spec in `services/common/README.md`.
7. **Secrets never enter git.** Copy `.env.example` to `.env`. Build vK8s Secrets from the templates in `deploy/vk8s/secrets/*.template.yaml` with `scripts/render_secrets.py`.
8. **Live demos must be deterministic.** Every agent supports `DEMO_SAFE_MODE`. When a user message matches a scenario trigger in `scenarios/*.yaml`, the runtime executes the scripted tool-call plan instead of trusting the model. The trace labels those steps `scripted`. Whether the model *chose* to misbehave doesn't matter here. What matters is what the fabric does when it does.
9. **Fabric errors are data, not crashes.** When XC returns 401, 403 or 429 to an agent, record it in the trace as `blocked_by: xc` with the status. Feed a short denial message back to the model, then continue or finish gracefully.

## Build order

Follow `docs/07-build-plan.md`:

1. Phase 0: AWS + CE + tenant
2. Phase 1: core services, runnable locally under `deploy/local/docker-compose.yaml`
3. Phase 2: deploy to vK8s
4. Phase 3: XC config
5. Phase 4: scenario hardening

Don't start Phase 3 until Phase 2's smoke test passes **through plain XC LBs with security features off**. Then turn the controls on one at a time and re-run the scenarios after each.

## Definition of done

- `make check` passes: lint, unit tests, policy/agent consistency.
- `scripts/smoke_test.py --env tenant` passes.
- `scripts/run_scenario.py --all --env tenant` passes every scenario's `expect:` block, three runs in a row.
- `scripts/measure_latency.py --env tenant` writes RE-to-CE p50/p95 to `results/latency.json`.
- `scripts/reset_demo.sh` restores seed state in under 60 seconds.

## Repo map

```
config/agents/           one YAML per agent (identity, model route, declared tools, prompt)
config/policy/           tool-access-matrix.yaml (SOURCE OF TRUTH)
config/tools/            tool schemas shared by MCP servers and agents
deploy/aws/              Terraform for VPC, EC2 "copperline-core" host, Bedrock VPC endpoint
deploy/local/            docker compose for laptop dev (no XC)
deploy/vk8s/             manifests for the agentic-demo vK8s namespace
deploy/xc/intent/        human-readable intent for every XC object (translate to Terraform)
deploy/xc/terraform/     Terraform you will write from the intent
docs/                    the spec
scenarios/               deterministic scenario plans and expected XC outcomes
scripts/                 policy generation, smoke test, scenario runner, latency, reset
services/                one folder per service, each with a README contract
```
