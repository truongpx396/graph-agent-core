"""The API's access log, one JSON line per request (RUNTIME_PATTERNS.md 112). It replaces uvicorn's, which was plain text with no id, no
route and no duration, and so never showed in the log dashboard.

Every response carries `X-Request-Id`. A queued chat route sets its own (the job id, which the worker's lines carry); every other
response gets one minted here. Either way `request_id` in the logs is the value the client was given.

Each request also starts a TRACE (RUNTIME_PATTERNS.md 113), named on the response in `X-Trace-Id`. It always starts a new one: a
client's `traceparent` is never read, because the trace id is also the Langfuse trace's id, and a caller who could choose it could write into
another request's record.
"""
import logging
import time
import uuid
from contextlib import AbstractContextManager, nullcontext

from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import log_context, spans
from app.core.errors import error_origin

logger = logging.getLogger(__name__)

_HEADER = "X-Request-Id"
_TRACE_HEADER = "X-Trace-Id"
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
        trace_id = None

        async def send_with_id(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                status, started = message["status"], True
                headers = MutableHeaders(scope=message)
                if _HEADER in headers:
                    log_context.add(request_id=headers[_HEADER])
                else:
                    headers[_HEADER] = log_context.current()["request_id"]
                if trace_id:
                    headers[_TRACE_HEADER] = trace_id
            await send(message)

        with _server_span(scope) as server, log_context.scope(request_id=minted):
            trace_id = spans.current_ids().get("trace_id")
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:
                if server is not None:
                    spans.failed(server, exc)
                # Where, not what (pattern 30). Re-raised: uvicorn still prints the traceback.
                logger.error("http_request_failed", extra={"error_class": type(exc).__name__, "origin": error_origin(exc)})
                if not started:
                    # Answered here so that a 500 carries its id too: Starlette's own handler sits outside every middleware.
                    await PlainTextResponse("Internal Server Error", status_code=500)(scope, receive, send_with_id)
                raise
            finally:
                _log_request(scope, status, time.monotonic() - start)
                if server is not None:
                    _describe(server, scope, status)


def _server_span(scope: Scope) -> AbstractContextManager[Span | None]:
    """A new root (`Context()`), whatever the request's headers say. Probes and static files are not traced."""
    if scope["path"].startswith(_QUIET_PREFIXES):
        return nullcontext()
    return spans.span(scope["method"], kind=SpanKind.SERVER, context=Context())


def _describe(server: Span, scope: Scope, status: int) -> None:
    """Named by the route once routing has run. The ids bound during the request make the trace searchable by them."""
    route = getattr(scope.get("route"), "path", None)
    if route:
        server.update_name(f"{scope['method']} {route}")
        server.set_attribute("http.route", route)
    server.set_attribute("http.request.method", scope["method"])
    server.set_attribute("url.path", scope["path"])
    server.set_attribute("http.response.status_code", status)
    if status >= 500:
        server.set_status(Status(StatusCode.ERROR))
    bound = log_context.current()
    for name in ("request_id", "thread_id", "tenant", "domain"):  # never the principal: it may be an email
        if name in bound:
            server.set_attribute(f"agent.{name}", bound[name])


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
