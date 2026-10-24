# Local mode (laptop or devbox, no XC)

`make local-up` starts the services with docker compose. Every XC load balancer runs as a **local-gateway** container, a small Python stand-in that reads the real XC intent files. Use local mode to develop agent, tool and IdP logic quickly. **Never use it to demonstrate anything.**

On this project, run it on the devbox: `make devbox-run CMD="make local-up"`, then `make devbox-run CMD="make local-check"`.

## Commands

| Command | What it does |
|---|---|
| `make local-up` | Creates keys and secrets if missing, stages config, builds the images and waits until every container is healthy |
| `make local-check` | Live checks against the running stack (`LOCAL_STACK=1 pytest -m live deploy/local`). See below |
| `make local-smoke` | `scripts/smoke_test.py --env local`: every hop answers, one chat end to end |
| `make cockpit` | The presenter UI on 127.0.0.1:8095 (on the devbox: then `make devbox-tunnel`) |
| `make local-scenarios` | `scripts/run_scenario.py --all --env local`: every scenario, checked against its `expect.local` (Phase 1 acceptance). `RUNS=3 make local-scenarios` for three runs in a row |
| `make local-down` | Stops everything and removes the volumes |
| `deploy/local/compose.sh <args>` | Any other compose command, e.g. `ps`, `logs -f tools-lb`. Use it instead of plain `docker compose`, which has no secrets or staged files |

## What runs

| Container | Plays | Published on 127.0.0.1 |
|---|---|---|
| `idp`, `erp-mock`, `ai-gw`, `partner-api`, `mcp-catalog`, `mcp-orders`, `mcp-inventory`, `mcp-refunds` | the services (partner-api through partner-lb: `127.0.0.1:8003/v1/…`) | erp-mock admin **8181** and ai-gw admin **8190** only |
| `shopper-agent`, `storeops-agent`, `refunds-agent` | the agents, reached through the front doors: `POST 127.0.0.1:8001/v1/chat`, `8002/storeops/v1/chat`, `8002/refunds/v1/chat` | none |
| `insights-nightly` | the shadow job (S5). Not started by `up`: `deploy/local/compose.sh run --rm insights-nightly` | none |
| `shop-lb`, `ops-lb`, `partner-lb` | the public front doors | **8001**, **8002**, **8003** |
| `tools-lb`, `llm-lb`, `idp-lb`, `erp-lb` | the internal LBs, on the network aliases `tools.agentic-demo`, `llm.agentic-demo`, `idp.agentic-demo` and `erp.agentic-demo`, port 80 | 8011, 8012, 8013, 8014 (debugging and tests only; nothing like them exists in XC) |

Services call the internal LBs with exactly the vK8s URLs, `http://idp.agentic-demo` and so on, so no code or config changes between local and tenant. The cockpit (`make cockpit`, http://127.0.0.1:8095) runs on the host, not in compose: it starts S5 with `compose.sh run`, and `cockpit-lb` is not emulated locally. See `services/cockpit/README.md`.

Every container runs the way vK8s will run it: `user: 1000770000:0`, read-only root filesystem, tmpfs `/tmp`, all capabilities dropped, not privileged, `no-new-privileges`. `make local-check` asserts each of these for all 18 containers. The internal-LB gateways listen on port 80 as non-root, through the per-container sysctl `net.ipv4.ip_unprivileged_port_start=0`. Every image that goes to vK8s listens on 8080.

## ai-gw: stub or real Bedrock

ai-gw runs in **stub mode** by default (canned replies, no AWS). To call real Bedrock, set these in the repo-root `.env` (or export them in the shell, which wins), then `make local-up` again:

```
LOCAL_STUB_MODEL=false
ROUTE_FAST=<Bedrock model or inference-profile ID>      # at least one route
ROUTE_STANDARD=...
AWS_REGION=us-east-1
```

`compose.sh` passes only these names (plus `BUDGET_TOKENS_*`) from `.env` to compose, via `local_env.py`. It refuses `LOCAL_STUB_MODEL=false` with no route. **Bedrock mode works on the devbox only**: ai-gw's credentials come from the instance role, and no AWS credentials are ever passed into a container. `curl -s 127.0.0.1:8190/v1/usage` shows `"mode": "stub"` or `"bedrock"`.

The devbox has no `.env`, and a sync deletes one created only on the box, so on the devbox pass the settings on the command line: `make devbox-run CMD="LOCAL_STUB_MODEL=false ROUTE_FAST=us.anthropic.claude-haiku-5-5 ROUTE_STANDARD=us.anthropic.claude-sonnet-5-5 ROUTE_PREMIUM=us.anthropic.claude-opus-5-5 make local-up"`, and `make devbox-run CMD="make local-up"` for stub mode again. Before pointing a route at a model never used in this account, make its first call from the SSO role (docs/08 R24): the devbox role cannot complete the model's Marketplace subscription.

## local-gateway: what it does, and what it does not

One process plays **one** XC load balancer (`LB_NAME`), from `deploy/xc/intent/20-load-balancers.yaml`, `service-policies.generated.yaml` and `30-security.yaml`. Origin pools map to compose services in `pools.yaml`. Per request, the first failure wins:

0. **Local safety checks**, not XC behavior: a method other than GET, HEAD, POST, PUT, PATCH, DELETE or OPTIONS → **405**; a path with `.` or `..` segments → **400**.
1. **JWT validation**, on the LB's `target_paths`: RS256 signature against `deploy/xc/generated/jwks.json`, `exp`, `iss`, `aud`, and mandatory claims present. Failure → **401**.
2. **Service policy**: rules in order, every matcher in a rule ANDed, first match wins, no match is deny. Matchers: path (exact or prefix), method, JWT claims, and request headers. A header sent twice never matches. Failure → **403**.
3. **Route**: first matching route, with `prefix_rewrite`. No route → **404**.
4. **Header changes** (`request_headers_to_remove`, then `request_headers_to_add`; the `aigw-api-key` secret comes from env `GATEWAY_API_KEY`), then proxy. Pool down → **503**, pool timeout → **504**, anything else failing while forwarding → **502**. The gateway keeps no cookies between requests, and forwards exactly one `x-request-id`: the caller's, or a generated one if it is missing or empty.

Every response the gateway makes itself is JSON, carries `x-local-gateway: <lb>`, never `x-copperline-origin`, and its body says `local-gateway — NOT XC`. `copperline_common.xcblock` then classifies:

- the gateway's **4xx** (400, 401, 403, 404, 405) as `blocked_by: xc`, reason starting `local-gateway (NOT XC)`;
- the gateway's **5xx** (502, 503, 504) as `blocked_by: network`, reason `upstream unreachable behind LB (HTTP …)`, per its rule for a 5xx behind an LB.

Responses from a pool pass through unchanged. Each request is logged as one `lb_request` line with `decision` (`allowed`, `denied`, or `upstream_error` when the policy allowed it but the pool failed), the matching `policy_rule`, and the `pool`.

**Not emulated** (each gateway names these at startup in `gateway_ready.not_emulated`): WAF, Data Guard, rate limiting, API definitions and OpenAPI validation, API and sensitive-data discovery, user identification, and malicious user detection. The `cockpit-lb` source-IP policy is not emulated either: locally the cockpit will be published on 127.0.0.1 with no gateway in front.

**Strict on purpose.** An intent field or policy matcher the gateway does not know stops it at startup with a message naming the field. A new XC control in the intent must be a conscious decision in `local-gateway/local_gateway/intent.py`: either emulate it, or list it as not emulated. The gateway never ignores it silently.

How closely this matches XC is unverified (docs/08 R6, R36–R38). A local pass says the services and the policy *intent* agree; only the tenant run says XC agrees.

## Files

| Path | Purpose |
|---|---|
| `docker-compose.yaml` | The stack. Each container gets only the secrets it needs |
| `compose.sh` | The wrapper. Runs `gen_keys.py --if-missing`, stages `.run/`, passes `.secrets/secrets.env` and the signing key as variables |
| `pools.yaml` | XC origin pool name → URL inside compose |
| `.run/` (gitignored) | World-readable copies of what the containers mount: `config/` (flat, like the vK8s ConfigMap), `gateway/` (intent files and `pools.yaml`), `keys/jwks.json` (the **public** key). Containers run as an arbitrary UID, and checkout files may be 0600 |
| `local-gateway/` | The gateway package, its Dockerfile and tests. `tests/test_scenarios_through_gateway.py` runs the real scenario plans through the gateway logic in-process (part of `make check`); `tests/test_live_stack.py` does the same against the containers (`make local-check`) |

Scenario files carry separate, looser `expect.local` blocks for this mode.
