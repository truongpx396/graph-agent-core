"""The API's access log, one JSON line per request (RUNTIME_PATTERNS.md 112). It replaces uvicorn's, which was plain text with no id, no
route and no duration, and so never showed in the log dashboard.

Every response carries `X-Request-Id`. A queued chat route sets its own (the job id, which the worker's lines carry); every other
response gets one minted here. Either way `request_id` in the logs is the value the client was given.
"""
import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import log_context
from app.core.errors import error_origin

logger = logging.getLogger(__name__)

_HEADER = "X-Request-Id"
# Probes every few seconds and the page's own files: worth a line only when they fail.
_QUIET_PREFIXES = ("/health", "/static/")


class RequestLogMiddleware:
    """Plain ASGI, not `BaseHTTPMiddleware`: the line is written when an SSE response ENDS (so `duration_ms` is the whole stream),
    and the handler stays in this context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        minted = uuid.uuid4().hex
        status, started = 500, False
        start = time.monotonic()

        async def send_with_id(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                status, started = message["status"], True
                headers = MutableHeaders(scope=message)
                if _HEADER in headers:
                    log_context.add(request_id=headers[_HEADER])
                else:
                    headers[_HEADER] = log_context.current()["request_id"]
            await send(message)

        with log_context.scope(request_id=minted):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:
                # Where, not what (pattern 30). Re-raised: uvicorn still prints the traceback.
                logger.error("http_request_failed", extra={"error_class": type(exc).__name__, "origin": error_origin(exc)})
                if not started:
                    # Answered here so that a 500 carries its id too: Starlette's own handler sits outside every middleware.
                    await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, send_with_id)
                raise
            finally:
                _log_request(scope, status, time.monotonic() - start)


def _log_request(scope: Scope, status: int, seconds: float) -> None:
    path = scope["path"]
    if status < 400 and path.startswith(_QUIET_PREFIXES):
        return
    logger.log(
        logging.ERROR if status >= 500 else logging.INFO,
        "http_request",
        # The route template groups requests; the path names the thing. Never the query string: it can carry a code or a person's text.
        extra={
            "method": scope["method"],
            "route": getattr(scope.get("route"), "path", None),
            "path": path,
            "status": status,
            "duration_ms": int(seconds * 1000),
        },
    )
