"""The API's access line and `X-Request-Id` (app/adapters/inbound/api/request_log.py, RUNTIME_PATTERNS.md 112). uvicorn's own access line
was plain text with no id, and no API line carried one."""
import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.adapters.inbound.api.deps import get_ctx, get_domain
from app.adapters.inbound.api.request_log import RequestLogMiddleware
from app.core.config import settings
from tests.fakes.json_logs import json_logs

HEADERS = {"X-Tenant-Id": "acme", "X-Principal-Id": "ada", "X-Domain": "support"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "headers")
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    @app.get("/things/{thing_id}")
    async def thing(thing_id: str, ctx=Depends(get_ctx), domain: str = Depends(get_domain)):
        return {"id": thing_id}

    @app.get("/job")
    async def job():
        return PlainTextResponse("ok", headers={"X-Request-Id": "job-42"})

    @app.get("/health/ready")
    async def ready(fail: bool = False):
        return PlainTextResponse("", status_code=503 if fail else 200)

    @app.get("/broken")
    async def broken():
        raise RuntimeError("postgres://user:hunter2@db/app is unreachable")

    return TestClient(app, raise_server_exceptions=False)


def _access(lines):
    return [line for line in lines() if line["message"] == "http_request"]


def test_the_line_has_the_route_the_status_the_caller_and_the_id_the_client_was_given(client):
    with json_logs() as lines:
        response = client.get("/things/7?code=SECRET", headers=HEADERS)

    (line,) = _access(lines)
    assert line["request_id"] == response.headers["X-Request-Id"]
    assert (line["method"], line["route"], line["path"], line["status"]) == ("GET", "/things/{thing_id}", "/things/7", 200)
    assert (line["tenant"], line["principal"], line["domain"]) == ("acme", "ada", "support")
    assert line["duration_ms"] >= 0
    assert "SECRET" not in str(line)  # the query string is never logged


def test_a_handlers_own_request_id_is_kept_and_logged(client):
    """A queued chat route answers with its JOB's id, which is what the worker's lines carry."""
    with json_logs() as lines:
        response = client.get("/job")

    assert response.headers.get_list("X-Request-Id") == ["job-42"]
    assert _access(lines)[0]["request_id"] == "job-42"


def test_two_requests_share_nothing(client):
    with json_logs() as lines:
        client.get("/things/1", headers=HEADERS)
        client.get("/job")

    first, second = _access(lines)
    assert first["request_id"] != second["request_id"]
    assert "tenant" not in second


def test_a_probe_is_logged_only_when_it_fails(client):
    with json_logs() as lines:
        client.get("/health/ready")
        client.get("/health/ready?fail=true")

    assert [line["status"] for line in _access(lines)] == [503]


def test_an_unhandled_error_answers_500_with_an_id_and_logs_where_not_what(client):
    with json_logs() as lines:
        response = client.get("/broken")

    assert response.status_code == 500
    failed = next(line for line in lines() if line["message"] == "http_request_failed")
    assert failed["request_id"] == response.headers["X-Request-Id"] == _access(lines)[0]["request_id"]
    assert failed["error_class"] == "RuntimeError"
    assert "hunter2" not in str(lines())
    assert _access(lines)[0]["level"] == "error"


def test_the_real_app_logs_every_request_including_one_a_middleware_refuses():
    from app.adapters.inbound.api.main import app

    assert app.user_middleware[0].cls is RequestLogMiddleware  # outermost
