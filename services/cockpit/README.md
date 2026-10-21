# cockpit: presenter UI and simulated partner agent (EC2, port 8095)

The cockpit is a single-page app (plain HTML + htmx or a small React build; keep it dependency-light) with a FastAPI backend. Its layout is described in `docs/03-services.md`.

## Backend responsibilities

- **Persona login.** Call `https://shop|ops.<DEMO_DOMAIN>/auth/demo-login` through the **public** XC LBs, so the user hop is governed too.
- **Chat.** POST to shop-lb or ops-lb `/…/v1/chat` and render `trace` as it arrives. Streaming isn't needed; a spinner per step is fine.
- **Scenario buttons.** Read `scenarios/*.yaml` and prefill persona and message. S3 shows two buttons (a, b). S7 sends its message `repeat_message` times.
- **S5 button.** Create a Job from `cronjob/insights-nightly` through the vK8s API (kubeconfig on EC2, read-only path), then tail the pod logs for `TRACE ` lines.
- **Partner tab.** Run the S6 plan directly from the backend, which plays the Granite Supply agent.
- **Estate panel.** Poll `127.0.0.1:8181/admin/state` (ERP) and `127.0.0.1:8190/v1/usage` (AI gateway admin port) every 2 s.
- **"Open in XC" links.** Taken from `XC_CONSOLE_LINKS` in `.env`.
- **Reset button.** Calls `scripts/reset_demo.sh` logic: ERP reset, gateway usage reset, new `RUN_SALT`.

## Visual language (match the deck)

Allowed steps are green, `blocked_by: xc` is F5 red (#E4002B), `server` and `network` are amber. Every row shows the status code and latency.

## Shared with `scripts/run_scenario.py` (decide in step 10)

The runner drives scenarios locally exactly the way this backend will on stage: log in at the front door with the run's `RUN_SALT`, POST `/…/v1/chat` with `scenario_hint`, read ERP `estate_delta` on loopback around the turn, send S7's message `repeat_message` times (one `front_door` row per attempt), run S5 as a Job and tail its `TRACE ` lines, play S6 with `plan_executor`. In the tenant only this backend can do all of that (the ERP admin port is on copperline-core's loopback), so `run_scenario.py --env tenant` (Phase 2) will call a backend endpoint rather than repeat the logic. Step 10 should move the driving code into one shared module both use, so the stage and the acceptance check can never drift apart.
