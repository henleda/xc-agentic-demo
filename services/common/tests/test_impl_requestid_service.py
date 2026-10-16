import asyncio

from copperline_common.requestid import (
    agent_id_var,
    current_request_id,
    new_request_id,
    obo_sub_var,
    request_id_var,
)
from copperline_common.service import setup_service
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app() -> FastAPI:
    app = FastAPI()
    setup_service(app, "svc-test")

    @app.get("/whoami")
    async def whoami():
        return {"request_id": request_id_var.get(), "agent_id": agent_id_var.get(), "obo_sub": obo_sub_var.get()}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/denied")
    async def denied():
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": "nope"}, status_code=403)

    return app


def test_healthz():
    client = TestClient(_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "svc-test"}
    assert response.headers["x-copperline-origin"] == "svc-test"


def test_request_id_is_echoed_or_generated():
    client = TestClient(_app())
    echoed = client.get("/whoami", headers={"x-request-id": "abc-123"})
    assert echoed.headers["x-request-id"] == "abc-123"
    assert echoed.json()["request_id"] == "abc-123"

    generated = client.get("/whoami")
    assert len(generated.headers["x-request-id"]) == 32
    assert generated.json()["request_id"] == generated.headers["x-request-id"]


def test_identity_from_agent_token(make_token):
    token = make_token(sub="shopper", agent_id="shopper", obo_sub="alex.rivera", obo_role="customer")
    body = TestClient(_app()).get("/whoami", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["agent_id"] == "shopper" and body["obo_sub"] == "alex.rivera"


def test_identity_from_user_token_uses_sub(make_token):
    token = make_token(sub="morgan.chen", role="care_supervisor")
    body = TestClient(_app()).get("/whoami", headers={"authorization": f"bearer {token}"}).json()
    assert body["agent_id"] is None and body["obo_sub"] == "morgan.chen"


def test_garbage_token_is_ignored():
    body = TestClient(_app()).get("/whoami", headers={"Authorization": "Bearer not.a.jwt"}).json()
    assert body["agent_id"] is None and body["obo_sub"] is None


def test_unhandled_exception_becomes_json_500_with_headers(read_logs):
    response = TestClient(_app()).get("/boom", headers={"x-request-id": "rid-boom"})
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"error": "internal_error"}
    assert response.headers["x-request-id"] == "rid-boom"
    assert response.headers["x-copperline-origin"] == "svc-test"
    records = read_logs()
    crash = [r for r in records if r.get("event") == "unhandled_exception"]
    assert crash and "kaboom" in crash[-1]["traceback"] and crash[-1]["request_id"] == "rid-boom"
    request_logs = [r for r in records if r.get("event") == "http_request" and r.get("path") == "/boom"]
    assert request_logs[-1]["status"] == 500


def test_http_request_logged_except_healthz(read_logs):
    client = TestClient(_app())
    client.get("/healthz")
    client.get("/denied", headers={"x-request-id": "rid-d"})
    records = [r for r in read_logs() if r.get("event") == "http_request"]
    assert [r["path"] for r in records] == ["/denied"]
    record = records[0]
    assert record["method"] == "GET" and record["status"] == 403 and record["request_id"] == "rid-d"
    assert isinstance(record["latency_ms"], int) and record["service"] == "svc-test"


def test_context_is_reset_after_request():
    client = TestClient(_app())
    client.get("/whoami", headers={"x-request-id": "leak-check"})
    assert request_id_var.get() is None


def test_current_and_new_request_id():
    async def inside_task():
        first = current_request_id()
        return first, current_request_id()

    first, second = asyncio.run(inside_task())
    assert first == second and len(first) == 32
    assert new_request_id("S4").startswith("S4-") and len(new_request_id("S4")) == 35
    assert "-" not in new_request_id()
