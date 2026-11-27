"""Tests for app/platform/logging_config.py's structlog-based structured
logging setup."""
import asyncio
import io
import json
import logging
import subprocess
import sys

import pytest

from app.core import log_context
from app.platform.logging_config import build_formatter


@pytest.fixture
def captured_logger():
    """A throwaway logger + handler pair writing JSON to an in-memory
    buffer — isolated from the real root logger (never calls
    configure_logging/basicConfig), so this can't interfere with pytest's
    own logging capture or leak a handler into other tests."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_formatter())
    logger = logging.getLogger("test.logging_config")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    yield logger, stream
    logger.removeHandler(handler)


def _last_line(stream: io.StringIO) -> dict:
    lines = stream.getvalue().strip().splitlines()
    return json.loads(lines[-1])


class TestStructlogFormatter:
    def test_basic_fields(self, captured_logger):
        logger, stream = captured_logger
        logger.info("something_happened")
        payload = _last_line(stream)
        assert payload["level"] == "info"  # structlog's own convention — lowercase
        assert payload["logger"] == "test.logging_config"
        assert payload["message"] == "something_happened"
        assert "timestamp" in payload

    def test_extra_fields_surface_as_real_json_keys(self, captured_logger):
        """The whole point (see this module's docstring): a plain
        `logging.basicConfig` formatter silently drops `extra=` fields —
        this one must not."""
        logger, stream = captured_logger
        logger.warning("tool_failed", extra={"tool": "search_docs", "run_id": "abc123"})
        payload = _last_line(stream)
        assert payload["tool"] == "search_docs"
        assert payload["run_id"] == "abc123"

    def test_non_json_native_extra_value_is_stringified_not_fatal(self, captured_logger):
        logger, stream = captured_logger
        logger.info("weird_value", extra={"code": ValueError("boom")})
        payload = _last_line(stream)
        assert "boom" in payload["code"]

    def test_exception_info_included(self, captured_logger):
        logger, stream = captured_logger
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            logger.exception("it_broke")
        payload = _last_line(stream)
        assert "kaboom" in payload["exc_info"]


class TestLogContext:
    def test_every_line_in_scope_carries_the_bound_ids(self, captured_logger):
        logger, stream = captured_logger
        with log_context.scope(request_id="req-1", thread_id="t-1", tenant="acme", principal=None):
            logger.info("inside")
        payload = _last_line(stream)
        assert (payload["request_id"], payload["thread_id"], payload["tenant"]) == ("req-1", "t-1", "acme")
        assert "principal" not in payload  # an unknown value is left out, never logged as "None"

    def test_nothing_is_bound_outside_a_scope(self, captured_logger):
        logger, stream = captured_logger
        with log_context.scope(request_id="req-1"):
            pass
        logger.info("after")
        assert "request_id" not in _last_line(stream)

    def test_an_explicit_extra_wins_over_the_bound_value(self, captured_logger):
        logger, stream = captured_logger
        with log_context.scope(request_id="ambient"):
            logger.warning("explicit", extra={"request_id": "explicit-value"})
        assert _last_line(stream)["request_id"] == "explicit-value"

    def test_a_nested_scope_adds_to_the_outer_one_and_restores_it(self, captured_logger):
        logger, stream = captured_logger
        with log_context.scope(request_id="outer", tenant="acme"):
            with log_context.scope(request_id="inner"):
                logger.info("nested")
                assert (_last_line(stream)["request_id"], _last_line(stream)["tenant"]) == ("inner", "acme")
            logger.info("back")
            assert _last_line(stream)["request_id"] == "outer"
        assert log_context.current() == {}

    def test_reset_happens_even_if_the_block_raises(self):
        with pytest.raises(ValueError), log_context.scope(request_id="req-1"):
            raise ValueError("boom")
        assert log_context.current() == {}

    async def test_what_a_child_task_adds_reaches_the_scope_that_opened_it(self):
        """The API's access line is written by the middleware, after dependencies that may run in a copied context."""

        async def learns_the_tenant():
            log_context.add(tenant="acme")

        with log_context.scope(request_id="req-1"):
            await asyncio.create_task(learns_the_tenant())
            assert log_context.current() == {"request_id": "req-1", "tenant": "acme"}

    def test_add_outside_a_scope_binds_nothing(self):
        log_context.add(tenant="acme")
        assert log_context.current() == {}


FAKE_TOKEN = "123456789:AAfakeTokenForTheTestsOnly_0123456789"  # the shape of a Bot API token, and no bot's


class TestNoSecretInALine:
    def test_a_bot_token_is_scrubbed_from_the_message_an_extra_and_the_traceback(self, captured_logger):
        logger, stream = captured_logger
        url = f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe"
        try:
            raise RuntimeError(f"401 for url '{url}'")
        except RuntimeError:
            logger.exception("call to %s failed", url, extra={"target": url})
        line = stream.getvalue()
        assert FAKE_TOKEN not in line
        assert "api.telegram.org/bot[REDACTED]/getMe" in line

    def test_a_short_configured_password_is_not_cut_out_of_ordinary_words(self, captured_logger, monkeypatch):
        monkeypatch.setattr("app.core.config.REDIS_URL", "redis://app:agent@redis:6379/0")
        logger, stream = captured_logger
        logger.info("node_completed", extra={"node": "agent"})
        assert _last_line(stream)["node"] == "agent"


def _run_configured(body: str) -> str:
    """`configure_logging` replaces the root handlers, so what it does is observed in a process of its own."""
    script = "import logging\nfrom app.platform.logging_config import configure_logging\n" + body
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True)
    return done.stderr


class TestConfigureLogging:
    def test_httpx_does_not_log_the_url_that_carries_the_bot_token(self):
        """The Telegram channel wrote its token on every poll: `httpx` logs each request's URL at INFO."""
        output = _run_configured(f'''
import asyncio, httpx
configure_logging()
async def main():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={{}}))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("https://api.telegram.org/bot{FAKE_TOKEN}/getUpdates", json={{}})
asyncio.run(main())
logging.getLogger("app").info("done")
''')
        assert FAKE_TOKEN not in output
        assert [json.loads(line)["message"] for line in output.splitlines()] == ["done"]

    def test_every_line_is_json_and_names_the_service_uvicorns_and_warnings_included(self):
        output = _run_configured('''
import warnings
plain = logging.getLogger("uvicorn.error")
plain.addHandler(logging.StreamHandler())  # what uvicorn does before it imports the app
plain.propagate = False
configure_logging(service="agent-core-api")
plain.warning("server line")
warnings.warn("careful")
''')
        lines = [json.loads(line) for line in output.splitlines()]
        assert [line["logger"] for line in lines] == ["uvicorn.error", "py.warnings"]
        assert {line["service"] for line in lines} == {"agent-core-api"}
