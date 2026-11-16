"""Tests for cooperative mid-turn cancellation (app/agent/runtime/core.py's
`cancel_check` parameter on `_iterate_with_timeout`/`_run_graph_stream`/
`astream_events_turn`) — the "actively streaming, not yet paused" half of
`POST /chat/cancel`'s two-mechanism design (GRAPH_PATTERNS.md pattern 43's
queue-first HITL). The other half — cancelling a turn already paused at
human_approval, via `cancel_run` — is already covered by
tests/agent/runtime/test_durable_checkpoint.py::TestAsyncEntryPoints; this file is the new
mechanism only.

Follows tests/agent/runtime/test_streaming_terminal_events.py's established pattern:
drive `astream_events_turn` against a hermetic fake-LLM graph via a
monkeypatched `init_graph_async`, bypassing the real durable checkpointer
(a different concern, covered elsewhere).
"""
import asyncio
import uuid
from itertools import pairwise

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph.build import GraphDeps
from app.agent.runtime import core as agent_module
from app.agent.runtime import stream as stream_module
from app.core import metrics
from app.core.errors import ErrorCode
from tests.conftest import TEST_CTX
from tests.conftest import metric_value as _count
from tests.ecorp import build_graph


def _slow_llm(text: str = "a longer answer with several tokens in it", delay: float = 0.05):
    """Same technique as tests/agent/runtime/test_concurrent_turns.py's own
    `_fixed_llm`/tests/agent/runtime/test_durable_checkpoint.py's `_SlowFakeLLM`:
    a real `await asyncio.sleep` between streamed chunks so there's an
    actual window to cancel INTO mid-stream, rather than the fake model
    returning before a test's own `task.cancel()` could ever land."""

    class _Model(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
                await asyncio.sleep(delay)

    return _Model(messages=iter([AIMessage(content=text)]))


class TestIterateWithTimeoutCancelCheck:
    """Unit-level: the wrapper itself, no graph involved."""

    async def test_raises_turn_cancelled_when_cancel_check_returns_true(self):
        async def aiter():
            yield 1
            yield 2

        async def always_cancelled():
            return True

        async def _run():
            events = []
            raised = None
            try:
                async for e in stream_module._iterate_with_timeout(
                    aiter(), 60, cancel_check=always_cancelled
                ):
                    events.append(e)
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below
                raised = exc
            return events, raised

        events, raised = await _run()
        assert events == []
        assert isinstance(raised, stream_module.TurnCancelledError)

    async def test_no_cancel_check_behaves_exactly_as_before(self):
        """Regression guard: every existing caller passes nothing here —
        must be unaffected by this parameter's addition."""

        async def aiter():
            yield 1
            yield 2

        async def _run():
            return [e async for e in stream_module._iterate_with_timeout(aiter(), 60)]

        assert await _run() == [1, 2]

    async def test_cancel_check_returning_false_does_not_interrupt_iteration(self):
        async def aiter():
            yield "a"
            yield "b"

        async def never_cancelled():
            return False

        async def _run():
            return [
                e
                async for e in stream_module._iterate_with_timeout(
                    aiter(), 60, cancel_check=never_cancelled
                )
            ]

        assert await _run() == ["a", "b"]


class TestTheCancelCheckIsPolledOncePerInterval:
    """The check is a Redis GET and a 500-token answer is 500 events: asking on every one was 500 round trips awaited one after another on the model's own
    stream (PRODUCTION_REVIEW.md R08, step 1). A stop now takes effect within a quarter of a second, at an event boundary, as before."""

    def _clock(self, monkeypatch):
        """A clock this test moves by hand, installed for the wrapper only (patching `time.monotonic` itself would stop the event loop)."""
        from types import SimpleNamespace

        now = [0.0]
        monkeypatch.setattr(stream_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
        return now

    async def test_it_asks_once_per_interval_not_once_per_event(self, monkeypatch):
        now, asked = self._clock(monkeypatch), []

        async def events():
            for i in range(20):
                yield i
                now[0] += 0.1  # an event every 100 ms: 2 seconds in all

        async def cancel_check():
            asked.append(now[0])
            return False

        assert [e async for e in stream_module._iterate_with_timeout(events(), 60, cancel_check=cancel_check)] == list(range(20))

        assert 2 <= len(asked) <= 9, f"asked {len(asked)} times for 20 events"
        assert all(later - earlier >= stream_module._CANCEL_POLL_INTERVAL_SECONDS for earlier, later in pairwise(asked))

    async def test_it_always_asks_before_the_first_event(self, monkeypatch):
        """`astream_events_resume`'s docstring relies on it: a stop that was already pressed is seen before anything runs."""
        self._clock(monkeypatch)
        order = []

        async def events():
            order.append("event")
            yield 1

        async def cancel_check():
            order.append("check")
            return False

        [e async for e in stream_module._iterate_with_timeout(events(), 60, cancel_check=cancel_check)]

        assert order[:2] == ["check", "event"]

    async def test_a_stop_pressed_mid_stream_takes_effect_at_the_first_event_boundary_after_the_interval(self, monkeypatch):
        now = self._clock(monkeypatch)
        seen, raised = [], None

        async def events():
            for i in range(20):
                yield i
                now[0] += 0.1

        async def cancel_check():
            return now[0] >= 0.5  # pressed at 0.5 s

        try:
            async for event in stream_module._iterate_with_timeout(events(), 60, cancel_check=cancel_check):
                seen.append(event)
        except stream_module.TurnCancelledError as exc:
            raised = exc

        assert raised is not None
        assert seen and seen[-1] * 0.1 < 0.5 + stream_module._CANCEL_POLL_INTERVAL_SECONDS + 0.2, "within about a quarter of a second of the press"

    async def test_with_no_cancel_check_nothing_is_polled_and_nothing_changes(self, monkeypatch):
        self._clock(monkeypatch)

        async def events():
            yield 1

        assert [e async for e in stream_module._iterate_with_timeout(events(), 60)] == [1]


def _fake_llm_multi_token_answer():
    return GenericFakeChatModel(
        messages=iter([AIMessage(content="a longer answer with several tokens in it")])
    )


class TestAstreamEventsTurnCancellation:
    """End-to-end through astream_events_turn/_run_graph_stream: a
    cancel_check flipping True mid-turn must surface as a terminal error
    event carrying ErrorCode.CANCELLED — modeled as a terminal-outcome
    envelope like any other (GRAPH_PATTERNS.md pattern 30), not a separate
    SSE event type — and never reach a normal "done"."""

    async def test_cancel_mid_stream_yields_a_cancelled_error_event_not_done(self, monkeypatch):
        # This test is about what a cancellation DOES, not how often it is polled: with the default quarter-second interval the short fake answer
        # would finish before a second check, so it asks on every event, as the wrapper used to.
        monkeypatch.setattr(stream_module, "_CANCEL_POLL_INTERVAL_SECONDS", 0)
        graph = build_graph(GraphDeps(llm=_fake_llm_multi_token_answer()))

        async def fake_init_graph_async():
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        # Cancel on the SECOND check — realistic "already streaming when
        # the user clicks Stop" timing, not "cancelled before anything
        # ever ran."
        seen = {"count": 0}

        async def cancel_after_first_check():
            seen["count"] += 1
            return seen["count"] > 1

        before = _count(metrics.agent_streaming_cancellation_total)

        async def _run():
            return [
                event
                async for event in stream_module.astream_events_turn(
                    "hello", str(uuid.uuid4()), TEST_CTX, cancel_check=cancel_after_first_check
                )
            ]

        events = await _run()

        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == ErrorCode.CANCELLED.value
        assert not any(e["type"] == "done" for e in events)
        assert _count(metrics.agent_streaming_cancellation_total) == before + 1

    async def test_cancel_check_that_never_fires_streams_normally_to_completion(self, monkeypatch):
        """A cancel_check present but always False must not change
        behavior — proves the mechanism is additive, not a new source of
        flakiness for the common (not cancelled) case."""
        graph = build_graph(GraphDeps(llm=_fake_llm_multi_token_answer()))

        async def fake_init_graph_async():
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        async def never_cancelled():
            return False

        async def _run():
            return [
                event
                async for event in stream_module.astream_events_turn(
                    "hello", str(uuid.uuid4()), TEST_CTX, cancel_check=never_cancelled
                )
            ]

        events = await _run()
        assert events[-1]["type"] == "done"

    async def test_no_cancel_check_streams_normally_to_completion(self, monkeypatch):
        """Regression guard: every caller without a cancel path (the unattended
        entry points a Telegram job runs through, and the scripts) passes no
        cancel_check at all — must behave exactly as before this feature existed."""
        graph = build_graph(GraphDeps(llm=_fake_llm_multi_token_answer()))

        async def fake_init_graph_async():
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        async def _run():
            return [
                event
                async for event in stream_module.astream_events_turn(
                    "hello", str(uuid.uuid4()), TEST_CTX
                )
            ]

        events = await _run()
        assert events[-1]["type"] == "done"


class TestAstreamEventsTurnRawAsyncioCancellation:
    """A GENUINE asyncio.CancelledError reaching _run_graph_stream — e.g.
    the caller's own task getting cancelled from outside (the ASGI layer
    tearing down a disconnected request, an asyncio.wait_for elsewhere
    timing out and cancelling this task as a side effect) — as opposed to
    TestAstreamEventsTurnCancellation above's TurnCancelledError, which only
    ever comes from cancel_check's own cooperative Redis-flag poll.

    Distinct code path on purpose: CancelledError is a BaseException
    (Python 3.8+), not an Exception subclass, SPECIFICALLY so a bare
    `except Exception` can't accidentally swallow it — which means it
    needs, and until this fix didn't have, its own except clause in
    _run_graph_stream. Verified live against a real load test before this
    fix existed: a raw CancelledError reaching this function skipped both
    except clauses, never called trace.update() or _record_turn_metrics,
    and the only thing that recorded anything was Langfuse's own
    CallbackHandler — independently wired into graph.astream_events()'s
    own callbacks — leaving "LangGraph" spans permanently open (no
    end_time, ever) with status_message set to raw asyncio internals
    ("<Task cancelled name=... coro=<AsyncExitStack.__aexit__()...>>"),
    not any message this app ever wrote."""

    async def test_cancelling_the_task_records_cancelled_metrics_and_still_propagates(
        self, monkeypatch
    ):
        graph = build_graph(GraphDeps(llm=_slow_llm()))

        async def fake_init_graph_async():
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        before_streaming = _count(metrics.agent_streaming_cancellation_total)
        before_requests_cancelled = _count(metrics.agent_requests_total, outcome="cancelled")

        async def _consume():
            async for _event in stream_module.astream_events_turn(
                "hello", str(uuid.uuid4()), TEST_CTX
            ):
                pass

        async def _run():
            task = asyncio.ensure_future(_consume())
            # Let the turn actually get INTO the graph/streaming before
            # cancelling — cancelling before it starts would prove
            # nothing about this specific except-branch, which only
            # matters once _run_graph_stream's own try block is live.
            await asyncio.sleep(0.05)
            task.cancel()
            raised = None
            try:
                await task
            except asyncio.CancelledError as exc:  # captured for the assertion below
                raised = exc
            return raised

        raised = await _run()

        # The whole point of re-raising: a genuine cancellation must still
        # actually cancel the caller, not be silently absorbed here.
        assert isinstance(raised, asyncio.CancelledError)
        assert _count(metrics.agent_streaming_cancellation_total) == before_streaming + 1
        assert (
            _count(metrics.agent_requests_total, outcome="cancelled")
            == before_requests_cancelled + 1
        )


class TestAstreamEventsResumeCancellation:
    """Real defect (fixed): `astream_events_resume` took no `cancel_check`, so
    once a user approved a paused tool call there was no way to stop the turn
    that followed — `POST /chat/cancel` set the Redis flag, but the worker
    running the *resume* job never polled it. The approved write, the model
    call that reads its result and any further tool rounds all ran to the end.
    Pause -> cancel was covered (`cancel_run`) and a streaming turn was
    covered (`cancel_check`); the stretch in between was the gap."""

    @staticmethod
    async def _paused_on_add_note(monkeypatch):
        """A real graph paused at the mandatory gate for an `add_note` call,
        with the tool's I/O stubbed to *record* writes — the observable that
        distinguishes "cancelled before the approved tool ran" from "ran,
        then reported cancelled"."""
        from app.adapters.outbound.qdrant import qdrant_store
        from app.domains.ecorp import store as ecorp_store

        writes: list = []

        async def fake_embed_text(text):
            return [0.0]

        async def fake_upsert(points):
            writes.append(points)

        monkeypatch.setattr(ecorp_store, "embed_document", fake_embed_text)
        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        llm = GenericFakeChatModel(
            messages=iter(
                [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "add_note",
                                "args": {"title": "Refunds", "content": "30 days.", "topic": "company"},
                                "id": "call_note",
                            }
                        ],
                    ),
                    AIMessage(content="I've added a note about the refund policy."),
                ]
            )
        )
        graph = build_graph(GraphDeps(llm=llm))

        async def fake_init_graph_async():
            return graph

        monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)

        thread_id = str(uuid.uuid4())
        cfg = {"configurable": {"thread_id": thread_id, "ctx": TEST_CTX}, "recursion_limit": 50}
        await graph.ainvoke({"messages": [HumanMessage(content="remember our refund policy")]}, config=cfg)
        assert (await graph.aget_state(cfg)).next, "premise: the run must be paused at the gate"
        return graph, thread_id, cfg, writes

    async def test_a_cancel_after_approval_stops_the_turn_before_the_approved_tool_runs(self, monkeypatch):
        graph, thread_id, cfg, writes = await self._paused_on_add_note(monkeypatch)

        async def always_cancelled():
            return True

        before = _count(metrics.agent_streaming_cancellation_total)
        events = [
            event
            async for event in stream_module.astream_events_resume(
                thread_id, approved=True, ctx=TEST_CTX, cancel_check=always_cancelled
            )
        ]

        assert events[-1]["type"] == "error"
        assert events[-1]["code"] == ErrorCode.CANCELLED.value
        assert not any(e["type"] == "done" for e in events)
        assert writes == [], "a cancelled approval must not perform the approved write"
        persisted = (await graph.aget_state(cfg)).values["messages"]
        assert not any(isinstance(m, ToolMessage) for m in persisted)
        assert _count(metrics.agent_streaming_cancellation_total) == before + 1

    async def test_a_cancel_check_that_never_fires_still_runs_the_approved_tool_once(self, monkeypatch):
        """The mechanism is additive: wiring `cancel_check` must not change
        what an approved, uncancelled resume does."""
        _graph, thread_id, _cfg, writes = await self._paused_on_add_note(monkeypatch)

        async def never_cancelled():
            return False

        events = [
            event
            async for event in stream_module.astream_events_resume(
                thread_id, approved=True, ctx=TEST_CTX, cancel_check=never_cancelled
            )
        ]

        assert events[-1]["type"] == "done"
        assert len(writes) == 1

    async def test_no_cancel_check_resumes_exactly_as_before(self, monkeypatch):
        """Regression guard: `astream_events_turn_unattended`, chat.py and the
        API all call `astream_events_resume(thread_id, approved=..., ctx=...)` with
        no `cancel_check` — their behavior must not change."""
        _graph, thread_id, _cfg, writes = await self._paused_on_add_note(monkeypatch)

        events = [event async for event in stream_module.astream_events_resume(thread_id, approved=True, ctx=TEST_CTX)]

        assert events[-1]["type"] == "done"
        assert len(writes) == 1
