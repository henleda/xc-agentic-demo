"""scripts/smoke_test.py against the real services in-process (the `stack` fixture). Healthy, it must say all ok, twice
in a row; with one hop broken, it must say FAIL on that hop. The step-9 review found the first version said "ok" for a
tool call that never reached the ERP, for stopped agents and partner-api, and for an erp-lb with no /admin/ rule.
"""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "scripts"))
import demo_env  # noqa: E402
import smoke_test  # noqa: E402

# The published local ports -> the in-process hostnames the stack fixture serves.
PORT_HOST = {8001: "shop-lb", 8002: "ops-lb", 8003: "partner-lb", 8011: "tools.agentic-demo",
             8012: "llm.agentic-demo", 8013: "idp.agentic-demo", 8014: "erp.agentic-demo",
             8181: "erp-mock-admin", 8190: "ai-gw-admin"}
STACK_SECRETS = {"AGENT_SHOPPER_SECRET": "s-shopper", "AGENT_STOREOPS_SECRET": "s-storeops",
                 "AGENT_REFUNDS_SECRET": "s-refunds", "SVC_MCP_SECRET": "s-svc", "GRANITE_SUPPLY_SECRET": "s-granite"}


class SyncOverStack(httpx.BaseTransport):
    """smoke_test uses a sync httpx.Client; the stack is async ASGI. Route 127.0.0.1:<port> to the right app."""

    def __init__(self, stack) -> None:
        self.stack = stack

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url.copy_with(scheme="http", host=PORT_HOST[request.url.port], port=None)

        async def go():
            response = await self.stack.http.request(request.method, url, headers=request.headers,
                                                     content=request.read())
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        return asyncio.run(go())


def run_smoke(stack, monkeypatch, capsys) -> tuple[int, str]:
    real_client = httpx.Client

    def client_on_stack(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=SyncOverStack(stack), **kwargs)

    monkeypatch.setattr(smoke_test.httpx, "Client", client_on_stack)
    monkeypatch.setattr(demo_env, "read_secrets", lambda: dict(STACK_SECRETS))
    capsys.readouterr()
    code = smoke_test.main(["--env", "local"])
    return code, capsys.readouterr().out


def failing_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip().startswith("FAIL")]


def test_a_healthy_stack_is_all_ok(stack, monkeypatch, capsys):
    code, out = run_smoke(stack, monkeypatch, capsys)
    assert code == 0 and "smoke: all ok" in out, out


def test_twice_in_a_row_is_still_all_ok(stack, monkeypatch, capsys):
    """The agent caches alex.rivera's tokens for minutes; the smoke must not depend on that cache."""
    assert run_smoke(stack, monkeypatch, capsys)[0] == 0
    code, out = run_smoke(stack, monkeypatch, capsys)
    assert code == 0, out


def test_an_erp_that_fails_every_request_is_a_fail(stack, monkeypatch, capsys):
    """The old smoke said all ok here: its tool call never reached the ERP."""
    async def broken(request):
        return JSONResponse({"error": "down"}, status_code=500, headers={"x-copperline-origin": "erp-mock"})
    stack.mesh.apps["erp-mock"] = Starlette(routes=[Route("/{p:path}", broken, methods=["GET", "POST"])])
    code, out = run_smoke(stack, monkeypatch, capsys)
    fails = failing_lines(out)
    assert code == 1
    assert any(line.startswith("FAIL  erp-lb -> erp-mock") for line in fails), fails
    assert any(line.startswith("FAIL  orders tool, which reads the ERP") for line in fails), fails


@pytest.mark.parametrize("app, line", [
    ("refunds-agent", "FAIL  ops-lb -> refunds agent"),
    ("storeops-agent", "FAIL  ops-lb -> storeops agent"),
    ("partner-api", "FAIL  partner-lb -> partner-api"),
    ("mcp-inventory", "FAIL  tools-lb -> mcp-inventory"),
    ("mcp-refunds", "FAIL  tools-lb -> mcp-refunds"),
])
def test_a_stopped_origin_is_a_fail_on_its_own_line(stack, monkeypatch, capsys, app, line):
    """The old smoke only pinged the gateways, so these all said ok."""
    del stack.mesh.apps[app]  # connection refused, as for a stopped container
    code, out = run_smoke(stack, monkeypatch, capsys)
    assert code == 1 and any(f.startswith(line) for f in failing_lines(out)), out


def test_an_erp_lb_without_the_admin_rule_is_a_fail(stack, monkeypatch, capsys):
    """The old check passed on any 401 for a tokenless request. Now: a valid service token must still get the
    gateway's 403 for /admin/. Simulate a policy that lets it through by routing erp-lb straight to the ERP."""
    stack.mesh.apps["erp.agentic-demo"] = stack.mesh.apps["erp-mock-admin"]
    code, out = run_smoke(stack, monkeypatch, capsys)
    assert any(f.startswith("FAIL  erp-lb refuses /admin/") for f in failing_lines(out)), out


def test_a_down_stack_is_fail_lines_not_a_traceback(monkeypatch, capsys):
    def refused(request):
        raise httpx.ConnectError("refused", request=request)
    real_client = httpx.Client
    monkeypatch.setattr(smoke_test.httpx, "Client",
                        lambda *a, **kw: real_client(transport=httpx.MockTransport(refused),
                                                     **{k: v for k, v in kw.items() if k != "transport"}))
    monkeypatch.setattr(demo_env, "read_secrets", lambda: dict(STACK_SECRETS))
    assert smoke_test.main(["--env", "local"]) == 1
    out = capsys.readouterr().out
    assert "make local-up" in out and "smoke: " in out
