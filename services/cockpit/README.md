# cockpit: presenter UI and simulated partner agent (EC2, port 8095)

One page (`cockpit/static/index.html`: plain HTML and JavaScript, no framework, no build step, nothing loaded from the internet, so it works offline on stage) and a small FastAPI backend. Layout as in `docs/03-services.md`: persona and chat on the left, the live trace in the middle, the Estate panel on the right, one button per scenario across the top.

## Run it

```bash
make local-up        # the stack (devbox: make devbox-run CMD="make local-up")
make cockpit         # serves http://127.0.0.1:8095 (loopback only)
```

On the devbox, run it there and open it on the laptop through the tunnel (`make devbox-tunnel`, then http://localhost:8095). To keep it running after the ssh session ends: `systemd-run --user --unit=copperline-cockpit --working-directory=$HOME/xc-agentic-demo $HOME/.local/bin/uv run python -m cockpit` (stop with `systemctl --user stop copperline-cockpit`).

`http://127.0.0.1:8095/?run=S3a` opens the page and runs that scenario: a deep link for rehearsals.

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

Runs are one at a time (a second click waits): two runs at once would mix their estate deltas.

## The same driving code as the acceptance check

Scenarios run through `copperline_common.demo_driver`, the same module `scripts/run_scenario.py` uses, and are judged the same way, so a green button and a green acceptance run mean the same thing:

- agent scenarios: log in at the front door with the run's salt, `POST /…/v1/chat` with `scenario_hint`, read the ERP counters around the turn;
- S7: the message `repeat_message` times, one front-door row per attempt;
- S5: the batch job. Locally `deploy/local/compose.sh run --rm insights-nightly`. **On EC2 (Phase 2, not built):** create a Job from `cronjob/insights-nightly` through the vK8s API (kubeconfig on EC2) and tail its `TRACE ` lines; until then the S5 button says so;
- S6 (the Partner tab): the backend plays Granite Supply, running the plan through partner-lb with its own client secret (kept out of the process environment);
- verdicts: PASS, FAIL, ERROR (could not run, ran another scenario, or the container runs another version of the plan: rebuild), or "no local expectations" (S7 locally).

## Visual language (match the deck)

Allowed steps are green, `blocked_by: xc` is F5 red (#E4002B) on a pale red row, `server` and `network` (and MCP tool errors) are amber. Every row shows the status code and latency. Token rows show the agent and the human it acts for, with both roles (`refunds (refunds) for sam.patel (care_agent)`), so S3's "same agent, different human" is on screen. Each scenario button shows the persona it runs as, so look-alike variants (S3a / S3b) are distinct.

## Env

| Env | Default | Meaning |
|---|---|---|
| `COCKPIT_ENV` | `local` | `local`: the compose stack's 127.0.0.1 ports. `tenant`: the public front doors on `DEMO_DOMAIN` (Phase 2) |
| `DEMO_DOMAIN` | — | Required for `tenant` |
| `XC_CONSOLE_LINKS` | `{}` | JSON object, scenario id → XC console URL: an "XC" link next to that button |
| `RUN_SALT` | random | The first run salt; Reset makes a new one |
| `PORT`, `COCKPIT_HOST` | `8095`, `127.0.0.1` locally | Listen address |

Locally the partner's client secret comes from `.secrets/secrets.env`; on EC2 from `.env` (`GRANITE_SUPPLY_SECRET`).
