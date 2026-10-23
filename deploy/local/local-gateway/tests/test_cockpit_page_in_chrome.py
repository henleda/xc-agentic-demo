"""The cockpit page as the audience sees it: the real page and backend against the real services in-process, rendered by
headless Chrome at 1280x720 (a projector), checked on the rendered DOM. Skipped where Chrome is not installed.

These pin what the step-10 review found missing on screen: who S3a acts for after S2 primed the agent's token cache,
S2's card number, S6's page reads collapsed to one row with method and path, scripted steps tagged, and failures
shown instead of a stuck spinner.
"""

import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
CHROME = next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                           shutil.which("google-chrome"), shutil.which("chromium")) if p and Path(p).exists()), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="headless Chrome is not installed here")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served_cockpit(cockpit_settings, in_process_job):
    """The real cockpit app under uvicorn on a free 127.0.0.1 port, talking to the in-process stack."""
    import uvicorn
    from cockpit.app import create_app

    app = create_app(cockpit_settings(run_salt="page1"), job_runner=in_process_job)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def rendered(url: str) -> str:
    """The DOM after the page's scripts ran (a deep link runs its scenario on load)."""
    out = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--window-size=1280,720",
                          "--virtual-time-budget=30000", "--dump-dom", url],
                         capture_output=True, text=True, timeout=120)
    return out.stdout


def text_of(dom: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", dom))


def test_s3a_after_s2_still_shows_who_the_agent_acts_for(served_cockpit):
    """The run of show does S2 then S3a, both as Sam: the agent's cached token means no token rows in S3a's trace."""
    import httpx

    httpx.post(f"{served_cockpit}/api/scenarios/S2/run", timeout=120)
    text = text_of(rendered(f"{served_cockpit}/?run=S3a"))
    assert "Acting: refunds agent, acting for sam.patel (care_agent)" in text
    assert "PASS" in text and "refunds.issue_refund" in text and "service policy" in text


def test_s2_shows_the_card_number_the_erp_returned(served_cockpit):
    text = text_of(rendered(f"{served_cockpit}/?run=S2"))
    assert "card_number: 4111111111111111" in text  # locally the raw PAN; in the tenant Data Guard's masked value


def test_s6_page_reads_are_one_row_and_http_rows_show_method_and_path(served_cockpit):
    text = text_of(rendered(f"{served_cockpit}/?run=S6"))
    assert "50 × GET /v1/purchase-orders" in text
    assert "GET /v1/internal/cost-sheet" in text and "POST /v1/asn" in text
    assert "Acting: granite-supply" in text


def test_scripted_steps_are_tagged_as_scripted(served_cockpit):
    dom = rendered(f"{served_cockpit}/?run=S1")
    rows = re.findall(r"<tr class=\"(?:ok|xc|amber)\">(.*?)</tr>", dom, re.S)
    refund = next(row for row in rows if "refunds.issue_refund" in row)
    assert ">script<" in refund


def test_the_deep_link_runs_once_and_removes_itself(served_cockpit):
    """A reload must never run a writing scenario again: the page drops ?run= from the address bar."""
    import httpx

    before = httpx.get(f"{served_cockpit}/api/estate", timeout=10).json()["erp"]["refunds_issued"]
    rendered(f"{served_cockpit}/?run=S3b")
    after = httpx.get(f"{served_cockpit}/api/estate", timeout=10).json()["erp"]["refunds_issued"]
    assert after == before + 1
    page = (REPO / "services/cockpit/cockpit/static/index.html").read_text()
    assert 'history.replaceState(null, "", location.pathname)' in page


def test_a_failing_backend_shows_an_error_not_a_stuck_spinner(tmp_path):
    """Any non-JSON answer (an LB error page) or an unreachable backend must end in a visible message."""
    page = (REPO / "services/cockpit/cockpit/static/index.html").read_text()
    fake_state = ('{env: "local", run_salt: "x", personas: [{sub: "a", name: "A", role: "customer", agent: "shopper"}],'
                  ' scenarios: [{id: "S0", title: "t", persona: "a", agent: "shopper", message: "m", acts_for: "x"}]}')
    fake_fetch = ('window.fetch = async (path) => (path === "/api/state" ? new Response(JSON.stringify(' + fake_state +
                  ')) : new Response("<html>502 Bad Gateway</html>", {status: 502}));')
    shim = page.replace('<script>\n"use strict";', '<script>\n"use strict";\n' + fake_fetch)
    (tmp_path / "index.html").write_text(shim + '<script>setTimeout(() => document.querySelector("button.scenario")'
                                                '.click(), 50);</script>')
    text = text_of(rendered((tmp_path / "index.html").as_uri()))
    assert "HTTP 502" in text and "running S0" not in text
