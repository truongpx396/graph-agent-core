"""Structured (JSON) logging, shared by every long-running service process (app/adapters/inbound/api/main.py, agent_worker.py,
ingest_worker.py, app/adapters/inbound/channels/telegram/bot.py), NOT the interactive CLI (app/adapters/inbound/channels/cli/chat.py) or one-shot
scripts, which print for a human and would get JSON noise interleaved.

Built on structlog, but no log call site changed: every call already goes through stdlib `logging` (`logger =
logging.getLogger(__name__)`) with per-call `extra={...}` correlation data. `configure_logging()` wires
structlog's `ProcessorFormatter` onto the ROOT logger's handler (the standard "structlog processes stdlib
logging" recipe), so every call site gets JSON rendering, the ids of the request or job in progress
(app/core/log_context.py), `trace_id`/`span_id` (app/core/spans.py), secret scrubbing and exception formatting for free.

Three problems this fixes without changing what's logged:
1. Plain `logging.basicConfig` silently drops every `extra` field.
2. app/adapters/inbound/api/main.py had no logging handler at all under `make serve`: Python discarded every `logger.info(...)`
   (only WARNING+ reached stderr, without `extra`).
3. `request_id`/`thread_id` was attached only at a turn's failure boundary, so every other log line of that
   turn had no correlation id. `log_context.scope` at the entry point plus a processor here fix it.

Output JSON keys: `timestamp`, `level` (lowercase, structlog convention, which Promtail/Loki expect), `logger`,
`message`, `service` when named, the bound ids, `extra=` fields, `exc_info` as a formatted traceback.

No secret in a line (RUNTIME_PATTERNS.md 112): `httpx` logged every URL at INFO, and the Telegram Bot API
carries the bot's token in the path, so the token was written to the logs on every poll. `httpx` is held at
WARNING and every string value is scrubbed, because an exception's own text can quote the same URL.
"""
import logging
import sys
import traceback
from typing import TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core import log_context, spans
from app.core.scrubbing import scrub_patterns

# Libraries whose INFO lines are one per outbound call, URL included: noise at best, a leaked token at worst (see the module docstring).
_QUIET_LOGGERS = ("httpx", "httpcore")
# Loggers uvicorn gives their own plain-text handlers before it imports the app; their lines belong on the root's JSON handler.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

_service: str | None = None


def _add_context(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: stamps the service name and the ids bound in `log_context` onto every event. A call site's own
    `extra={...}` wins, so this must run AFTER `structlog.stdlib.ExtraAdder`."""
    if _service is not None:
        event_dict.setdefault("service", _service)
    for name, value in {**log_context.current(), **spans.current_ids()}.items():
        event_dict.setdefault(name, value)
    return event_dict


def _scrub_secrets(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Patterns only, not the bound-secret values `scrub` also replaces: a short development password (`agent`, `postgres`)
    would be cut out of logger names and node names."""
    for name, value in event_dict.items():
        if isinstance(value, str):
            event_dict[name] = scrub_patterns(value)
    return event_dict


def _rename_event_to_message(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog's own convention is an `event` key; this app's log
    consumers expect `message`, matching the previous formatter."""
    event_dict["message"] = event_dict.pop("event")
    return event_dict


def _format_exc_info(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Renders the exc_info 3-tuple ProcessorFormatter attaches (from
    `logger.exception(...)`/`exc_info=True`) into a formatted traceback
    string under the same `exc_info` key — never the raw, non-JSON-
    serializable tuple."""
    exc_info = event_dict.pop("exc_info", None)
    if exc_info:
        if exc_info is True:
            exc_info = sys.exc_info()
        event_dict["exc_info"] = "".join(traceback.format_exception(*exc_info))
    return event_dict


# Shared by real log records (via ProcessorFormatter's foreign_pre_chain)
# and any native structlog call (none today, but wrap_for_formatter still
# needs this chain configured via structlog.configure() below).
_SHARED_PROCESSORS: list[Processor] = [
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
    structlog.stdlib.ExtraAdder(),
    _add_context,
    _rename_event_to_message,
]


def build_formatter() -> structlog.stdlib.ProcessorFormatter:
    """Exposed separately from `configure_logging` so tests can attach it to
    an isolated, non-propagating logger instead of the real root logger."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _format_exc_info,
            _scrub_secrets,
            # default=str: an extra field is sometimes non-JSON-native (Enum,
            # UUID, ...) — stringify rather than crash the whole log line.
            structlog.processors.JSONRenderer(default=str),
        ],
    )


class _DynamicStreamHandler(logging.StreamHandler[TextIO]):
    """`logging.StreamHandler()` resolves `sys.stderr` ONCE at construction
    and holds that reference — breaks if stderr is later swapped/closed
    (surfaced by pytest's per-test capture: a finalizer logging during
    shutdown raised "I/O operation on closed file" against an already-closed
    capture stream). Re-resolving `sys.stderr` on every emit avoids this —
    also relevant to a production log-rotation setup that reopens fds.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(level: int = logging.INFO, *, service: str | None = None) -> None:
    """Call once, at process startup, before anything logs. `service` is stamped on every line (the same name the process
    gives its telemetry), so a line read outside Loki still says who wrote it. `force=True`
    replaces any handler a prior import already attached to the root logger
    (e.g. a third-party `basicConfig` call) so this app's JSON formatting
    wins regardless of import order. Freely re-callable (unlike
    telemetry.py::configure_telemetry) — each call just replaces the root
    handlers again.
    """
    structlog.configure(
        processors=[*_SHARED_PROCESSORS, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    global _service
    _service = service

    handler = _DynamicStreamHandler()
    handler.setFormatter(build_formatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.captureWarnings(capture=True)  # a `warnings.warn` was several plain-text lines
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
