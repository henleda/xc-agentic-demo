"""cockpit backend. Contract: services/cockpit/README.md and docs/03-services.md ("cockpit").

    GET  /                          the page (static/index.html)
    GET  /api/state                 personas, scenarios (buttons), console links, run salt, env, what is running now
    POST /api/scenarios/{id}/run    run one scenario the way the stage does -> {verdict, trace, reply, checks, ...}
    POST /api/chat                  one turn as a persona (rehearsal; on stage use the scenario buttons)
    GET  /api/estate                ERP counters and AI gateway usage (polled every 2 s by the page)
    POST /api/reset                 ERP reset, gateway usage reset, new run salt

Scenarios run through copperline_common.demo_driver, the same code scripts/run_scenario.py uses, and are judged the
same way (demo_driver.judge_env), so what the presenter sees and what the acceptance check checks never drift apart.

One thing at a time: while a scenario, a chat or a reset runs, another one is refused with 409 "busy" (and the page
says what is running) instead of being queued. A queued click would still run minutes later, after the presenter had
moved on or reloaded: a second refund, or a reset in the middle of the next beat. The estate poll is never blocked.

Scenarios and personas are re-read when their files change, so a plan edit reaches the buttons without a restart.
"""

import asyncio
import secrets
import subprocess
import time
from pathlib import Path

import httpx
from copperline_common import demo_driver
from copperline_common.logging import log_event
from copperline_common.outbound import new_http_client, new_sync_http_client
from copperline_common.personas import Personas
from copperline_common.scenarios import by_id, load_scenarios
from copperline_common.service import setup_service
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from cockpit.settings import Settings

SERVICE = "cockpit"
STATIC = Path(__file__).resolve().parent / "static"
JOB_TIMEOUT_S = 300
AGENT_FOR_ROLE = {"customer": "shopper", "store_associate": "storeops"}  # everyone else: the refunds desk
MAX_CHAT_MESSAGES = 50


class Busy:
    """What is running now (one thing at a time). Plain attributes: one event loop owns them."""

    def __init__(self) -> None:
        self.what: str | None = None
        self.started = 0.0

    def state(self) -> dict | None:
        return {"what": self.what, "seconds": int(time.monotonic() - self.started)} if self.what else None


def create_app(settings: Settings, *, job_runner: demo_driver.JobRunner | None = None) -> FastAPI:
    """job_runner: tests pass a fake; otherwise compose locally, and (Phase 2) a vK8s Job in the tenant."""
    app = FastAPI(title="copperline-cockpit", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.files = ScenarioFiles(settings)  # raises RuntimeError if there are no scenarios: never start empty
    app.state.run_salt = settings.run_salt or secrets.token_hex(4)
    app.state.busy = Busy()
    app.state.job_runner = job_runner or default_job_runner(settings)
    setup_service(app, SERVICE)
    app.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/state", get_state, methods=["GET"])
    app.add_api_route("/api/scenarios/{scenario_id}/run", run_scenario, methods=["POST"])
    app.add_api_route("/api/chat", chat, methods=["POST"])
    app.add_api_route("/api/estate", get_estate, methods=["GET"])
    app.add_api_route("/api/reset", reset, methods=["POST"])
    log_event("cockpit_ready", env=settings.env, scenarios=[s.id for s in app.state.files.scenarios()],
              job_mode=settings.job_mode, scenarios_dir=str(settings.scenarios_dir))
    return app


class ScenarioFiles:
    """scenarios/*.yaml and personas.yaml, re-read whenever one of the files changes (mtime)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stamp: tuple | None = None
        self._scenarios: list = []
        self._personas: Personas | None = None
        self._reload()
        if not self._scenarios:
            raise RuntimeError(f"no scenarios in {settings.scenarios_dir} (set SCENARIOS_DIR)")

    def _files(self) -> list[Path]:
        return sorted(self.settings.scenarios_dir.glob("*.yaml")) + [self.settings.personas_path]

    def _reload(self) -> None:
        stamp = tuple((str(path), path.stat().st_mtime_ns) for path in self._files() if path.exists())
        if stamp != self._stamp:
            self._scenarios = load_scenarios(self.settings.scenarios_dir)
            self._personas = Personas.load(self.settings.personas_path)
            if self._stamp is not None:
                log_event("scenarios_reloaded", scenarios=[s.id for s in self._scenarios])
            self._stamp = stamp

    def scenarios(self) -> list:
        self._reload()
        return self._scenarios

    def personas(self) -> Personas:
        self._reload()
        return self._personas


def default_job_runner(settings: Settings) -> demo_driver.JobRunner | None:
    if settings.job_mode != "compose" or settings.compose_script is None:
        return None  # TODO(Phase 2): create a Job from cronjob/insights-nightly via the vK8s API and tail its log

    def compose_job(name: str) -> tuple[int, str, str]:
        try:
            done = subprocess.run([str(settings.compose_script), "run", "--rm", "-T", name], capture_output=True,
                                  text=True, timeout=JOB_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:  # keep what the job printed before it hung (its TRACE lines so far)
            partial = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return 124, partial, f"the job did not finish within {JOB_TIMEOUT_S} s"
        return done.returncode, done.stdout, done.stderr

    return compose_job


def busy_answer(busy: Busy) -> JSONResponse:
    state = busy.state()
    return JSONResponse({"error": "busy", "detail": f"{state['what']} is running ({state['seconds']} s)",
                         "running": state}, status_code=409)


async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


async def get_state(request: Request) -> dict:
    app = request.app
    files: ScenarioFiles = app.state.files
    personas = [{"sub": p["sub"], "name": p["name"], "role": p["role"],
                 "agent": AGENT_FOR_ROLE.get(p["role"], "refunds")} for p in files.personas().personas]
    by_sub = {p["sub"]: p for p in personas}
    scenarios = []
    for s in files.scenarios():
        persona = by_sub.get(s.persona)
        scenarios.append({"id": s.id, "title": s.title, "persona": s.persona, "agent": s.agent,
                          "message": s.message, "repeat_message": s.repeat_message,
                          # who the run acts for, shown above its trace (a cached agent token adds no token row)
                          "acts_for": acting_pair(s, persona),
                          "console_link": app.state.settings.console_links.get(s.id)})
    return {"env": app.state.settings.env, "run_salt": app.state.run_salt, "personas": personas,
            "scenarios": scenarios, "job_mode": app.state.settings.job_mode, "running": app.state.busy.state()}


def acting_pair(scenario, persona: dict | None) -> str:
    if scenario.persona.startswith("job:"):
        return f"{scenario.agent} job, copied storeops credential, no human behind it"
    if scenario.persona.startswith("partner:"):
        return f"{scenario.persona.removeprefix('partner:')}, a third-party agent with its own client credentials"
    if persona is None:
        return scenario.persona
    return f"{scenario.agent} agent, acting for {persona['sub']} ({persona['role']})"


async def run_scenario(scenario_id: str, request: Request) -> JSONResponse:
    app = request.app
    files: ScenarioFiles = app.state.files
    scenario = by_id(files.scenarios(), scenario_id)
    if scenario is None:
        return JSONResponse({"error": "not_found", "detail": f"no scenario {scenario_id!r}"}, status_code=404)
    busy: Busy = app.state.busy
    if busy.what:
        return busy_answer(busy)
    settings: Settings = app.state.settings
    busy.what, busy.started = scenario_id, time.monotonic()
    try:
        # The driver is blocking (httpx sync, subprocess): keep it off the event loop so the estate poll still answers.
        outcome = await run_in_threadpool(demo_driver.run_one, scenario, settings.endpoints, files.personas(),
                                          app.state.run_salt, app.state.job_runner)
    finally:
        busy.what = None
    # The same judge env the runner uses (demo_driver.judge_env), so both judge every scenario alike.
    result = demo_driver.judge(scenario, settings.env, outcome, demo_driver.judge_env(settings.endpoints))
    log_event("scenario_run", scenario=scenario.id, verdict=result["verdict"], steps=len(result["trace"]),
              run_salt=app.state.run_salt)
    return JSONResponse({**result, "run_salt": app.state.run_salt})


def _bad(detail: str) -> JSONResponse:
    return JSONResponse({"error": "bad_request", "detail": detail}, status_code=400)


async def chat(request: Request) -> JSONResponse:
    """One turn as a persona: {sub, messages[, scenario_hint]}. Rehearsal only; the stage uses scenario buttons."""
    app = request.app
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        return _bad("body is not JSON")
    if not isinstance(body, dict):
        return _bad("body must be a JSON object")
    persona = app.state.files.personas().get(str(body.get("sub", "")))
    messages = body.get("messages")
    if persona is None:
        return _bad("sub must be a demo persona")
    if (not isinstance(messages, list) or not 0 < len(messages) <= MAX_CHAT_MESSAGES or not all(
            isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
            for m in messages)):
        return _bad(f"messages must be 1 to {MAX_CHAT_MESSAGES} {{role, content}} objects")
    hint = body.get("scenario_hint")
    if hint is not None and not isinstance(hint, str):
        return _bad("scenario_hint must be a string")
    busy: Busy = app.state.busy
    if busy.what:
        return busy_answer(busy)
    agent = AGENT_FOR_ROLE.get(persona["role"], "refunds")
    endpoints: demo_driver.Endpoints = app.state.settings.endpoints

    def turn() -> dict:
        with new_sync_http_client(timeout=demo_driver.CHAT_TIMEOUT_S) as client:
            token, problem = demo_driver.login(client, endpoints, persona["sub"], persona["role"], app.state.run_salt)
            if token is None:
                return {"error": problem, "trace": []}
            response, started = demo_driver.chat(client, endpoints, agent, persona["role"], token, messages, hint)
        if response.status_code != 200:
            return {"error": f"chat answered HTTP {response.status_code}",
                    "trace": [demo_driver.front_door_row(1, response, started)]}
        try:
            return response.json()
        except ValueError:
            return {"error": "the agent's answer is not JSON", "trace": []}

    busy.what, busy.started = f"chat as {persona['sub']}", time.monotonic()
    try:
        result = await run_in_threadpool(turn)
    except httpx.HTTPError as e:
        result = {"error": f"unreachable: {type(e).__name__} (is the stack up?)", "trace": []}
    finally:
        busy.what = None
    return JSONResponse(result)


async def get_estate(request: Request) -> dict:
    """ERP counters and recent writes, and AI gateway token usage. A missing source is null, never an error."""
    endpoints: demo_driver.Endpoints = request.app.state.settings.endpoints
    async with new_http_client(timeout=2) as client:
        erp, usage = await asyncio.gather(_get_json(client, _url(endpoints.erp_admin_url, "/admin/state")),
                                          _get_json(client, _url(endpoints.aigw_admin_url, "/v1/usage")))
    return {"erp": erp, "ai_gateway": usage, "run_salt": request.app.state.run_salt,
            "running": request.app.state.busy.state()}


def _url(base: str | None, path: str) -> str | None:
    return f"{base}{path}" if base else None


async def _get_json(client: httpx.AsyncClient, url: str | None) -> dict | None:
    if not url:
        return None  # that source is not configured here
    try:
        response = await client.get(url)
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def reset(request: Request) -> JSONResponse:
    """What scripts/reset_demo.sh will do: ERP back to seed, gateway usage to zero, a new salt for Mallory."""
    busy: Busy = request.app.state.busy
    if busy.what:
        return busy_answer(busy)
    endpoints: demo_driver.Endpoints = request.app.state.settings.endpoints
    busy.what, busy.started = "reset", time.monotonic()
    try:
        async with new_http_client(timeout=10) as client:
            erp = await _post_ok(client, _url(endpoints.erp_admin_url, "/admin/reset"))
            usage = await _post_ok(client, _url(endpoints.aigw_admin_url, "/v1/usage/reset"))
        request.app.state.run_salt = secrets.token_hex(4)
    finally:
        busy.what = None
    log_event("demo_reset", erp=erp, ai_gateway=usage, run_salt=request.app.state.run_salt)
    return JSONResponse({"erp": erp, "ai_gateway": usage, "run_salt": request.app.state.run_salt})


async def _post_ok(client: httpx.AsyncClient, url: str | None) -> bool:
    if not url:
        return False
    try:
        return (await client.post(url)).status_code == 200
    except httpx.HTTPError:
        return False
