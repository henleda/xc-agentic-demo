# cockpit: presenter UI and simulated partner agent (EC2, port 8095)

One page (`cockpit/static/index.html`: plain HTML and JavaScript, no framework, no build step, nothing loaded from the internet, so it works offline on stage) and a small FastAPI backend. Layout as in `docs/03-services.md`: persona and chat on the left, the live trace in the middle, the Estate panel on the right, one button per scenario across the top.

## Run it

```bash
make local-up        # the stack (devbox: make devbox-run CMD="make local-up")
make cockpit         # serves http://127.0.0.1:8095 (loopback only)
```

On the devbox, run it there and open it on the laptop through the tunnel (`make devbox-tunnel`, then http://localhost:8095). To keep it running after the ssh session ends, run it as a user service, once enabling "linger" so the service outlives your sessions:

```bash
sudo loginctl enable-linger ubuntu      # once per box
systemd-run --user --unit=copperline-cockpit --working-directory=$HOME/xc-agentic-demo \
  $HOME/.local/bin/uv run python -m cockpit
systemctl --user restart copperline-cockpit     # after pulling changes;  stop: systemctl --user stop copperline-cockpit
```

(`compose.sh` finds uv through `$UV`, which `uv run` sets, so the S5 button works there although `~/.local/bin` is not on a service's PATH.)

`http://127.0.0.1:8095/?run=S3a` opens the page and runs that scenario once, then drops `?run=` from the address bar so a reload never runs it again: a deep link for rehearsals.

Locally the cockpit runs on the host, not in compose: it starts S5 with `deploy/local/compose.sh run`, and `cockpit-lb` (a source-IP allow list) is not emulated locally. On EC2 it runs from `deploy/aws/core/docker-compose.yaml` with host networking, behind `cockpit-lb`.

## Backend API

| Endpoint | What it does |
|---|---|
| `GET /` | The page |
| `GET /api/state` | Personas, scenarios (the buttons), "Open in XC" links, the current run salt, the environment |
| `POST /api/scenarios/{id}/run` | Runs one scenario the way the stage does and judges it: `{verdict, trace, reply, checks, error, estate_delta, run_salt}` |
| `POST /api/chat` | One turn as a persona, `{sub, messages[, scenario_hint]}`. For rehearsal: on stage, use the buttons (`docs/06`: never type a free prompt) |
| `GET /api/estate` | ERP counters and last writes (`127.0.0.1:8181/admin/state`), AI gateway usage (`127.0.0.1:8190/v1/usage`). The page polls it every 2 s, also while a scenario runs. A source that is down is `null` |
| `POST /api/reset` | ERP back to seed, gateway usage to zero, a new run salt (Mallory logs in as `mallory.x-<salt>`, so XC's malicious-user state never carries over) |

One thing at a time: while a scenario, a chat or a reset runs, another click is refused with 409 "busy" and the page says what is running and for how long. Nothing is queued: a queued click would still run after the presenter had moved on (a second refund, or a reset in the middle of the next beat). `/api/state` and `/api/estate` report what is running, so a reloaded page shows it. Edits to `scenarios/` and `personas.yaml` are picked up on the next click, with no restart.

## The same driving code as the acceptance check

Scenarios run through `copperline_common.demo_driver`, the same module `scripts/run_scenario.py` uses, and are judged the same way, so a green button and a green acceptance run mean the same thing:

- agent scenarios: log in at the front door with the run's salt, `POST /…/v1/chat` with `scenario_hint`, read the ERP counters around the turn;
- S7: the message `repeat_message` times, one front-door row per attempt;
- S5: the batch job. Locally `deploy/local/compose.sh run --rm insights-nightly`. **On EC2 (Phase 2, not built):** create a Job from `cronjob/insights-nightly` through the vK8s API (kubeconfig on EC2) and tail its `TRACE ` lines; until then the S5 button says so;
- S6 (the Partner button): the backend plays Granite Supply, running the plan through partner-lb with its own client secret (kept out of the process environment, and the only secret the cockpit holds);
- verdicts: PASS, FAIL, ERROR (could not run, ran another scenario, or the agents run another version of the plan: rebuild), or "no local expectations" (S7 locally). The cockpit and `run_scenario.py` judge with the same `demo_driver.judge_env`.

## Visual language (match the deck)

Allowed steps are green, `blocked_by: xc` is F5 red (#E4002B) on a pale red row, `server` and `network` (and MCP tool errors) are amber. Every row shows the status code and latency, on one line; a blocked row wraps so its reason is read in full, and the first blocked row is scrolled into view (S4's first 429). Laid out for a 1280×720 projector.

- **Acting:** above every trace, who the run acts for: "refunds agent, acting for sam.patel (care_agent)". It comes from the scenario, not from a token row, so S3a shows it even when the agent reuses Sam's token from S2 (no exchange, no token row).
- **by:** who decided each step: `script` (the scenario's plan, CLAUDE.md rule 8), `model` (the model chose it), `agent` (the agent's own token exchange), `user` (S7's front-door attempts). With real Bedrock this keeps S1's injected refund call from looking like the model's choice.
- **step:** tool names; `GET /v1/internal/cost-sheet` for http steps. Consecutive repeats of one http request (S6's 50 page reads) are one row with a count per status ("20 × 200, 30 × 429"); S4's 40 tool calls stay one row each, because they are the beat.
- **detail:** for a tool, the one field its beat is about, never cut off: S2's `card_number` (Data Guard's masked value in the tenant), S3b's `refund_id`, S4's `store_id`. The full result is on hover. Failures and the reasons behind a FAIL or ERROR are shown above the table.
- Each scenario button shows the persona it runs as, so look-alike variants (S3a / S3b) are distinct. Reset clears the page too.

## Env

| Env | Default | Meaning |
|---|---|---|
| `COCKPIT_ENV` | `local` (`tenant` in the image) | `local`: the compose stack's 127.0.0.1 ports, from a checkout. `tenant`: the public front doors on `DEMO_DOMAIN`; refuses to start without `DEMO_DOMAIN` and `GRANITE_SUPPLY_SECRET` |
| `SCENARIOS_DIR`, `PERSONAS_PATH` | the checkout's; `/app/scenarios`, `/app/config/personas.yaml` in the image | Where the buttons come from. An empty scenarios directory stops the cockpit at startup |
| `DEMO_DOMAIN` | — | Required for `tenant` |
| `XC_CONSOLE_LINKS` | `{}` | JSON object, scenario id → XC console URL: an "XC" link next to that button |
| `RUN_SALT` | random | The first run salt; Reset makes a new one |
| `PORT`, `COCKPIT_HOST` | `8095`, `127.0.0.1` locally | Listen address |

Locally the partner's client secret comes from `.secrets/secrets.env` (only that one secret is read); on EC2 from `.env` (`GRANITE_SUPPLY_SECRET`, which `gen_keys.py` keeps equal to the one in the vK8s Secret).

## On EC2 (Phase 2)

`services/cockpit/Dockerfile` builds the image (scenarios and personas baked in, `USER 10001`, `COCKPIT_ENV=tenant`), and `deploy/aws/core/docker-compose.yaml` runs it with host networking: it listens on 8095 on all interfaces (cockpit-lb's origin is the host's private IP) and reaches the ERP and AI gateway admin ports on 127.0.0.1. Still to build in Phase 2: the S5 button's vK8s Job (kubeconfig mounted at `/kube/config`), and cockpit-lb's route timeout (docs/08 R44).
