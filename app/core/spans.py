"""Distributed tracing with OpenTelemetry: where a request's TIME went, across the API, the queue and a worker (RUNTIME_PATTERNS.md 113).

Not Langfuse. `llm_tracing.py` records what the model was sent and answered; a span here records names, timings, counts and outcomes,
never a message, an argument, a result or an exception's text. There is ONE id: the W3C trace id is `trace_id` on every log line and
`X-Trace-Id` on a response, and the turn's Langfuse trace is created with the same id.

Our own `TracerProvider`, never the process-global one: `mcp`, `langsmith` and `litellm` start spans of their own on the global provider,
and what they put in them is not ours to vouch for. Until `install` is called (tests, the CLI) everything here is a no-op.
"""
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import AttributeValue

# The queued job's key for the W3C context of the request that published it.
CARRIER_KEY = "trace_context"

_tracer: trace.Tracer = trace.NoOpTracer()
_propagator = TraceContextTextMapPropagator()


def install(tracer: trace.Tracer) -> None:
    """Called once by app/platform/telemetry.py (and by a test that wants to see the spans)."""
    global _tracer
    _tracer = tracer


def current_ids() -> dict[str, str]:
    """`trace_id` and `span_id` of the span in progress, as the W3C writes them; empty when there is none."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": trace.format_trace_id(context.trace_id), "span_id": trace.format_span_id(context.span_id)}


def failed(span: Span, error: BaseException) -> None:
    """Mark a span as an error by the CLASS of what went wrong. Never `record_exception`: the message and traceback can quote
    a person's text, a host or a DSN."""
    span.set_attribute("error.type", type(error).__name__)
    span.set_status(Status(StatusCode.ERROR))


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    context: Context | None = None,
    links: Sequence[Link] | None = None,
    attributes: Mapping[str, AttributeValue] | None = None,
    expected: tuple[type[BaseException], ...] = (),
) -> Iterator[Span]:
    """A span that is current for the block. `expected` exceptions are control flow (a graph pausing), not errors."""
    with _tracer.start_as_current_span(
        name, context=context, kind=kind, links=links, attributes=attributes, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except expected:
            current.set_attribute("agent.paused", value=True)
            raise
        except Exception as exc:
            failed(current, exc)
            raise  # re-raised untouched: this only marks the span


def carrier() -> dict[str, str]:
    """The current context as W3C headers, to travel with a queued job. Empty when there is nothing to carry."""
    headers: dict[str, str] = {}
    _propagator.inject(headers)
    return headers


@contextmanager
def job_span(name: str, payload: Mapping[str, Any], *, retried: bool, attributes: Mapping[str, AttributeValue]) -> Iterator[Span]:
    """A worker's span for one queued job, in the trace of the request that published it: one trace from the HTTP call to the answer.
    A job RUN AGAIN after a crash starts a trace of its own, linked to that one, so two attempts are never written into one."""
    headers = payload.get(CARRIER_KEY)
    published = _propagator.extract(headers if isinstance(headers, dict) else {}, context=Context())
    publisher = trace.get_current_span(published).get_span_context()
    context = Context() if retried else published
    links = [Link(publisher)] if retried and publisher.is_valid else None
    with span(name, kind=SpanKind.CONSUMER, context=context, links=links, attributes=attributes) as current:
        yield current


class SpanCallbackHandler(BaseCallbackHandler):
    """A span per model call and per tool run, under whatever span is current (the graph node's). Names and token counts only
    (OpenTelemetry's GenAI attribute names)."""

    run_inline = True  # on the caller's task, so the current span is the node's

    def __init__(self) -> None:
        self._open: dict[UUID, Span] = {}

    def _start(self, run_id: UUID, name: str, kind: SpanKind, attributes: Mapping[str, AttributeValue]) -> None:
        self._open[run_id] = _tracer.start_span(name, kind=kind, attributes=attributes)

    def _end(self, run_id: UUID, error: BaseException | None = None) -> None:
        current = self._open.pop(run_id, None)
        if current is None:
            return
        if error is not None:
            failed(current, error)
        current.end()

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], *, run_id: UUID, metadata: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        model = str((metadata or {}).get("ls_model_name") or "")
        attributes: dict[str, AttributeValue] = {"gen_ai.operation.name": "chat"}
        if model:
            attributes["gen_ai.request.model"] = model
        self._start(run_id, f"chat {model}".strip(), SpanKind.CLIENT, attributes)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        current = self._open.get(run_id)
        if current is not None:
            first = response.generations[0][0] if response.generations and response.generations[0] else None
            usage = getattr(getattr(first, "message", None), "usage_metadata", None)
            if usage:
                current.set_attribute("gen_ai.usage.input_tokens", int(usage.get("input_tokens", 0)))
                current.set_attribute("gen_ai.usage.output_tokens", int(usage.get("output_tokens", 0)))
        self._end(run_id)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, error)

    def on_tool_start(self, serialized: dict[str, Any] | None, input_str: str, *, run_id: UUID, **kwargs: Any) -> None:
        name = str((serialized or {}).get("name") or "tool")
        self._start(run_id, f"execute_tool {name}", SpanKind.INTERNAL, {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name})

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, error)
