"""One trace from the HTTP request, across the queue, to the model and tool calls, with one id and no content in it
(app/core/spans.py, RUNTIME_PATTERNS.md 113)."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from app.adapters.inbound.api.request_log import RequestLogMiddleware
from app.adapters.inbound.workers import agent_worker
from app.adapters.outbound.redis import queue
from app.agent.graph.build import GraphDeps
from app.agent.runtime import core as runtime_module
from app.agent.runtime import stream as stream_module
from app.core import gateway, spans
from app.ports.outbound.jobs import requests_stream_key
from tests.adapters.outbound.redis.test_queue import FakeRedis
from tests.agent.runtime.test_held_answer import _calculate, _Streams
from tests.ecorp import build_graph
from tests.fakes.json_logs import json_logs

CTX = {"tenant": "acme", "principal": "ada@example.com", "claims": {}}
QUESTION = "What is two plus two, my confidential question?"


@pytest.fixture
def finished(monkeypatch):
    """The spans that end during the test. The tracer is ours, so nothing global is touched."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(spans, "_tracer", provider.get_tracer("test"))
    return exporter.get_finished_spans


def _api(client: FakeRedis) -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    @app.post("/chat")
    async def chat():
        await queue.publish_request(client, request_id="r1", text=QUESTION, thread_id="t1", ctx=CTX)
        return {}

    return TestClient(app)


def _published(client: FakeRedis) -> tuple[str, dict]:
    entry_id, fields = client.streams[requests_stream_key("ecorp")][-1]
    return entry_id, fields


def _named(finished, prefix: str):
    return [span for span in finished() if span.name.startswith(prefix)]


async def test_one_trace_holds_the_request_the_job_its_nodes_and_the_model_and_tool_calls(finished, monkeypatch):
    model = _Streams(messages=iter([]), script=[_calculate(), AIMessage(content="Two plus two is four, as the calculator says.")])
    graph = build_graph(GraphDeps(llm=model))

    async def fake_init():
        return graph

    monkeypatch.setattr(runtime_module, "init_graph_async", fake_init)
    client = FakeRedis()
    response = _api(client).post("/chat")
    entry_id, fields = _published(client)

    with json_logs() as lines:
        await agent_worker.process_request(client, entry_id, fields, domain="ecorp")

    (server,) = [span for span in finished() if span.kind is SpanKind.SERVER]
    (job,) = _named(finished, "agent_job turn")
    trace_id = trace.format_trace_id(server.context.trace_id)
    assert response.headers["X-Trace-Id"] == trace_id
    assert job.parent.span_id == server.context.span_id
    assert {span.context.trace_id for span in finished()} == {server.context.trace_id}

    nodes = {span.name: span for span in _named(finished, "node ")}
    assert {"node validate_input", "node agent", "node tools"} <= set(nodes)
    assert all(span.parent.span_id == job.context.span_id for span in nodes.values())
    chats = _named(finished, "chat")
    (tool,) = _named(finished, "execute_tool calculator")
    assert len(chats) == 2 and {c.parent.span_id for c in chats} <= {span.context.span_id for span in _named(finished, "node agent")}
    assert tool.parent is not None

    job_lines = [line for line in lines() if line["message"] in ("agent_job_started", "node_completed", "tool_called")]
    assert job_lines and {line["trace_id"] for line in job_lines} == {trace_id}

    recorded = json.dumps([[span.name, dict(span.attributes), [str(e.attributes) for e in span.events]] for span in finished()])
    for secret in (QUESTION, "confidential", "2+2", "calculator says", "ada@example.com"):
        assert secret not in recorded


def test_a_callers_traceparent_is_ignored(finished):
    """The trace id is also the Langfuse trace's id: a caller who could choose it could write into another request's record."""
    chosen = "0af7651916cd43dd8448eb211c80319c"
    response = _api(FakeRedis()).post("/chat", headers={"traceparent": f"00-{chosen}-b7ad6b7169203331-01"})
    assert response.headers["X-Trace-Id"] != chosen
    assert all(span.parent is None for span in finished() if span.kind is SpanKind.SERVER)


def test_a_job_run_again_starts_its_own_trace_linked_to_the_first(finished):
    with spans.span("request") as request:
        payload = {spans.CARRIER_KEY: spans.carrier()}
    with spans.job_span("agent_job turn", payload, retried=True, attributes={}):
        pass

    (job,) = _named(finished, "agent_job")
    assert job.context.trace_id != request.get_span_context().trace_id
    assert [link.context.trace_id for link in job.links] == [request.get_span_context().trace_id]


@pytest.mark.parametrize("carrier", [None, "not-a-dict", {"traceparent": "garbage"}])
def test_a_job_with_no_usable_context_still_gets_a_trace(finished, carrier):
    with spans.job_span("agent_job turn", {spans.CARRIER_KEY: carrier}, retried=False, attributes={}):
        assert spans.current_ids()


def test_a_failure_is_marked_by_its_class_and_a_pause_is_not_a_failure(finished):
    with pytest.raises(RuntimeError), spans.span("broken"):
        raise RuntimeError("postgres://user:hunter2@db is unreachable")
    with pytest.raises(GraphInterrupt), spans.span("paused", expected=(GraphInterrupt,)):
        raise GraphInterrupt()

    broken, paused = finished()
    assert (broken.status.status_code, broken.attributes["error.type"], list(broken.events)) == (StatusCode.ERROR, "RuntimeError", [])
    assert broken.status.description is None
    assert (paused.status.status_code, paused.attributes["agent.paused"]) == (StatusCode.UNSET, True)


def test_with_no_exporter_installed_nothing_is_added_anywhere():
    client = FakeRedis()
    with json_logs() as lines:
        response = _api(client).post("/chat")
    assert "X-Trace-Id" not in response.headers
    assert spans.CARRIER_KEY not in json.loads(_published(client)[1]["payload"])
    assert all("trace_id" not in line for line in lines())


def test_the_langfuse_trace_has_the_same_id_and_a_later_run_does_not_rename_it(finished):
    """An unattended turn that declines an approval runs the graph twice in one request. Langfuse upserts on id, so the second
    run must not send a name or an input: they would replace the person's message with `resume(...)`."""
    with spans.span("request"):
        trace_id = spans.current_ids()["trace_id"]
        first = stream_module._trace_fields("chat-turn-stream", "t1", "the person's message")
        second = stream_module._trace_fields("chat-turn-stream-resume", "t1", "resume(approved=False)")
    with spans.span("next request"):
        third = stream_module._trace_fields("chat-turn-stream-resume", "t1", "resume(approved=True)")

    assert first["id"] == second["id"] == trace_id
    assert first["input"] == "the person's message" and first["metadata"]["trace_id"] == trace_id
    assert "input" not in second and "name" not in second
    assert third["id"] != trace_id and third["input"] == "resume(approved=True)"


def test_without_a_trace_langfuse_chooses_its_own_id():
    fields = stream_module._trace_fields("chat-turn-stream", "t1", "hello")
    assert "id" not in fields and fields["input"] == "hello"


def test_the_proxy_is_never_told_a_trace_id():
    """LiteLLM reads `trace_id` and `existing_trace_id` from a call's metadata and would write its own record of the call into the
    app's Langfuse trace, counting every call's tokens twice."""
    with spans.span("request"):
        sent = gateway.call_identity(CTX, session_id="t1")
    assert not {"trace_id", "existing_trace_id"} & set(sent["extra_body"]["metadata"])
