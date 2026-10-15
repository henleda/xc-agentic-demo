# cockpit: presenter UI and simulated partner agent (EC2, port 8095)

The cockpit is a single-page app (plain HTML + htmx or a small React build; keep it dependency-light) with a FastAPI backend. Its layout is described in `docs/03-services.md`.

## Backend responsibilities

- **Persona login.** Call `https://shop|ops.<DEMO_DOMAIN>/auth/demo-login` through the **public** XC LBs, so the user hop is governed too.
- **Chat.** POST to shop-lb or ops-lb `/…/v1/chat` and render `trace` as it arrives. Streaming isn't needed; a spinner per step is fine.
- **Scenario buttons.** Read `scenarios/*.yaml` and prefill persona and message. S3 shows two buttons (a, b). S7 sends its message `repeat_message` times.
- **S5 button.** Create a Job from `cronjob/insights-nightly` through the vK8s API (kubeconfig on EC2, read-only path), then tail the pod logs for `TRACE ` lines.
- **Partner tab.** Run the S6 plan directly from the backend, which plays the Granite Supply agent.
- **Estate panel.** Poll `127.0.0.1:8181/admin/state` (ERP) and `127.0.0.1:8090/v1/usage` (AI gateway) every 2 s.
- **"Open in XC" links.** Taken from `XC_CONSOLE_LINKS` in `.env`.
- **Reset button.** Calls `scripts/reset_demo.sh` logic: ERP reset, gateway usage reset, new `RUN_SALT`.

## Visual language (match the deck)

Allowed steps are green, `blocked_by: xc` is F5 red (#E4002B), `server` and `network` are amber. Every row shows the status code and latency.
