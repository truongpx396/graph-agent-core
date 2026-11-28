"""OpenTelemetry metrics for the agent runtime.

Pushed via OTLP to a shared otel-collector (telemetry.py::configure_telemetry), which exposes one aggregated
Prometheus target for the API and every scaled agent_worker.py/ingest_worker.py replica (pattern 43). A
pull-based `GET /metrics` on the API alone could never see a worker's metrics, since nothing scrapes a worker.
Rates come from PromQL in Grafana, not from stored values.

`Counter`/`Histogram` below are a prometheus_client-shaped wrapper over the real OTel API (`.add`/`.record`),
so existing call sites (`x.labels(k=v).inc()` / `x.observe(v)`) needed no rewrite. Histogram bucket boundaries
aren't set here (OTel has no per-call bucket param); they are Views on the MeterProvider in telemetry.py,
matched by instrument name.

Two ways they are incremented: tool calls/errors through `MetricsCallbackHandler` (wired into
`config["callbacks"]` like the Langfuse handler, so the nodes in app/agent/graph/core.py need no
instrumentation), and everything else directly where it happens, in those nodes or at
app/agent/runtime/core.py's turn boundary, since those events can fire more than once per turn or need the
final state dict, which is awkward to rebuild from generic callback events.
"""
import hashlib
import logging
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import metrics as metrics_api

logger = logging.getLogger(__name__)

# A proxy meter (see telemetry.py): every create_counter/create_histogram
# call below is safe before configure_telemetry() runs, and gets replayed
# against the real MeterProvider once it does.
_meter = metrics_api.get_meter("app.core.metrics")


def _fingerprint(text: str) -> str:
    """A short, stable fingerprint of `text` for an audit log line — NEVER
    the raw content (pattern 14's "never message content in a generic log"
    rule, extended to tool args/results). Enough to correlate "was this the
    same result as last time" without an unscrubbed copy sitting outside
    Langfuse."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


class _BoundCounter:
    __slots__ = ("_attributes", "_instrument")

    def __init__(self, instrument: metrics_api.Counter, attributes: Mapping[str, str]):
        self._instrument = instrument
        self._attributes = attributes

    def inc(self, amount: float = 1) -> None:
        self._instrument.add(amount, attributes=self._attributes)


class Counter:
    """prometheus_client.Counter-shaped wrapper around an OTel Counter."""

    def __init__(self, name: str, description: str = "", labelnames: Iterable[str] = ()) -> None:
        self.name = name  # read by tests/core/test_metrics.py to look up this
        # instrument's recorded data points via an InMemoryMetricReader —
        # OTel instruments don't expose their own name back once created.
        self._instrument = _meter.create_counter(name, description=description)
        self._labelnames = tuple(labelnames)

    def labels(self, **kwargs: str) -> _BoundCounter:
        return _BoundCounter(self._instrument, kwargs)

    def inc(self, amount: float = 1) -> None:
        self._instrument.add(amount)


class _BoundHistogram:
    __slots__ = ("_attributes", "_instrument")

    def __init__(self, instrument: metrics_api.Histogram, attributes: Mapping[str, str]):
        self._instrument = instrument
        self._attributes = attributes

    def observe(self, value: float) -> None:
        self._instrument.record(value, attributes=self._attributes)


class Histogram:
    """prometheus_client.Histogram-shaped wrapper around an OTel Histogram.
    Bucket boundaries live in app/platform/telemetry.py's Views, not here."""

    def __init__(self, name: str, description: str = "", unit: str = "", labelnames: Iterable[str] = ()) -> None:
        self._instrument = _meter.create_histogram(
            name, description=description, unit=unit
        )
        self._labelnames = tuple(labelnames)

    def labels(self, **kwargs: str) -> _BoundHistogram:
        return _BoundHistogram(self._instrument, kwargs)

    def observe(self, value: float) -> None:
        self._instrument.record(value)


class _BoundGauge:
    __slots__ = ("_attributes", "_instrument")

    # `Any`: this OpenTelemetry release exposes the synchronous Gauge only under a private name (`_Gauge`), while
    # `Meter.create_gauge` that returns it is public; naming the private class here would break on the next release.
    def __init__(self, instrument: Any, attributes: Mapping[str, str]):
        self._instrument = instrument
        self._attributes = attributes

    def set(self, value: float) -> None:
        self._instrument.set(value, attributes=self._attributes)


class Gauge:
    """prometheus_client.Gauge-shaped wrapper around an OTel synchronous Gauge: the LAST value set wins, which is what a
    "how old is the oldest unsent thing right now" reading needs and a Counter cannot say."""

    def __init__(self, name: str, description: str = "", unit: str = "", labelnames: Iterable[str] = ()) -> None:
        self.name = name
        self._instrument = _meter.create_gauge(name, description=description, unit=unit)
        self._labelnames = tuple(labelnames)

    def labels(self, **kwargs: str) -> _BoundGauge:
        return _BoundGauge(self._instrument, kwargs)

    def set(self, value: float) -> None:
        self._instrument.set(value)


agent_requests_total = Counter(
    "agent_requests_total", "Total agent turns by outcome", ["outcome"]
)  # outcome: success | rejected | error | timeout | cancelled

agent_latency_seconds = Histogram(
    "agent_latency_seconds",
    "End-to-end latency per agent turn, in seconds",
    unit="s",
)

agent_iterations = Histogram(
    "agent_iterations",
    "LLM loop iterations per turn",
)

agent_tokens_total = Counter(
    "agent_tokens_total",
    "Cumulative tokens consumed, summed per turn from usage_metadata "
    "(0 if the model/proxy doesn't report it)",
)

agent_tool_calls_total = Counter(
    "agent_tool_calls_total", "Tool calls issued by the LLM", ["tool"]
)

agent_tool_errors_total = Counter(
    "agent_tool_errors_total", "Tool calls that raised and were recovered by handle_tool_errors"
)

agent_human_approval_total = Counter(
    "agent_human_approval_total", "HITL approval decisions", ["decision"]
)  # decision: approved | rejected

agent_retry_total = Counter(
    "agent_retry_total", "Output-quality retries triggered by graph_edge_route_after_check_output"
)

agent_stream_restarted_total = Counter(
    "agent_stream_restarted_total",
    "A model stream failed after it had sent tokens and the `agent` node's retry policy ran it again; the "
    "client was told to discard what it had shown (pattern 7). Distinct from agent_retry_total, which "
    "counts output-quality retries of a stream that ended normally.",
)

agent_output_check_fired_total = Counter(
    "agent_output_check_fired_total",
    "An output check (graph/output_checks.py: too_short, deferred, uncited, ...) fired on a finished answer, by the model tier that wrote it (`local` when the "
    "router is off or kept the turn local, else `medium` or `strong`). Many of those checks exist because a 3B or 7B local model drops instructions, and a "
    "false positive costs another full model round; this says which ones still earn their place once ROUTER_MODE sends turns to a stronger model. The "
    "per-check counters (agent_deferred_instead_of_acting_total, ...) stay: alerts and dashboards use them. PRODUCTION_REVIEW.md R32.",
    ["check", "tier"],
)  # check: a code in output_checks.CHECKS, or "misattributed" (repaired, not retried); tier: local | medium | strong

agent_context_gap_reask_total = Counter(
    "agent_context_gap_reask_total",
    "Second looks that ended in an answer (check_output's context_gap, docs/GRAPH_PATTERNS.md pattern 105): `looked_up` when a tool ran and its answer "
    "is shown, `restored` when none did and the first answer was put back. A mostly `restored` rate means the extra model call is not buying anything.",
    ["outcome"],
)  # outcome: looked_up | restored

agent_zero_citations_total = Counter(
    "agent_zero_citations_total",
    "Non-empty final answers with retrieved context available but zero "
    "citation markers used (check_output) — the opposite failure mode "
    "from ungrounded_claims_count (citing something not backed by a "
    "source, vs. using sourced content without citing it at all). "
    "Directional only, like ungrounded_claims_count: a legitimate "
    "general-knowledge or calculator-only answer looks identical, since "
    "retrieve_context always returns its top-K docs regardless of "
    "relevance — not acted on by graph_edge_route_after_check_output, just observed.",
)

agent_misattributed_citations_total = Counter(
    "agent_misattributed_citations_total",
    "Final answers where a real, in-range citation marker was used on a "
    "sentence whose content shares no meaningful vocabulary with that "
    "marker's actual source text (check_output's "
    "\"misattributed\" check) — a real marker attached to unsupported content, as opposed "
    "to a fabricated marker (ungrounded_claims_count) or a real source used "
    "with no marker at all (the opposite failure, the \"uncited\" check). "
    "Repaired in place: check_output drops the marker from those sentences "
    "(citations.drop_citation_markers) and the answer stands, with no retry "
    "(docs/GRAPH_PATTERNS.md pattern 104). Also counted in "
    "agent_output_check_fired_total{check=\"misattributed\"}.",
)

agent_dangling_citations_total = Counter(
    "agent_dangling_citations_total",
    "Final answers that carried a citation with nothing behind it: `[n]` markers in a turn with no numbered source at all, or a bracket naming one "
    "of the domain's tools. check_output removes it and the answer stands (citations.py, docs/GRAPH_PATTERNS.md pattern 104).",
)

agent_reference_footer_stripped_total = Counter(
    "agent_reference_footer_stripped_total",
    "Final answers where check_output stripped a fabricated markdown-style "
    "reference-list footer ('[1]: some link') the model appended after its "
    "own inline [n] markers (see strip_fabricated_reference_footer) — this "
    "app's citation convention is inline-only, so any such footer's 'link' "
    "is always invented, never a real source the model was actually given.",
)

agent_closing_offer_stripped_total = Counter(
    "agent_closing_offer_stripped_total",
    "Final answers where check_output cut a closing offer (\"Would you like more information or "
    "should I proceed...?\") off a complete answer instead of rejecting the answer for it (see "
    "strip_closing_offer). The system prompt forbids that question and the small local model "
    "writes it anyway, so a steady rate is the model ignoring the prompt, not a fault in the graph.",
)

agent_citation_auto_inserted_total = Counter(
    "agent_citation_auto_inserted_total",
    "Final answers where check_output mechanically inserted a missing [n] "
    "marker into the specific sentence the \"uncited\" check flagged, "
    "instead of retrying the model over it (see "
    "insert_missing_citation_markers's own docstring) — live-verified "
    "that asking the model to fix this itself (the standard reminder, six "
    "reworded variants, and the actual concrete retry-feedback message) "
    "reliably does not work on a real case, so this is the primary "
    "correction path for an uncited answer now, not a fallback. "
    "Watch this alongside the \"uncited\" check firing at all: a rising "
    "rate here means the model is drifting toward citing less, even though "
    "each individual case still gets silently corrected.",
)

agent_deferred_instead_of_acting_total = Counter(
    "agent_deferred_instead_of_acting_total",
    "Final answers that narrate an intent to use a tool ('I will use the X "
    "tool...') or ask the user's permission to proceed ('would you like me "
    "to?') instead of actually calling the tool or answering directly "
    "(check_output's defers_instead_of_acting) — real bug, found live: a "
    "3B model repeating this across several turns on the same thread, "
    "each 'yes' reply just restarting the identical unresolved cycle since "
    "no real tool_calls were ever made. Acted on by graph_edge_route_after_check_output: "
    "triggers a retry with feedback to call the tool now, not describe it.",
)

agent_fabricated_tool_output_total = Counter(
    "agent_fabricated_tool_output_total",
    "Final answers containing two or more markdown code fences with NO "
    "real tool_calls entry backing them (check_output's "
    "fabricates_tool_output) — a script AND a plausible-looking 'output' "
    "for it, presented as if run_command_in_sandbox had actually run, when "
    "it never did. Real bug, found live: after a sandbox approval was "
    "declined once, the model invented both a script and its output, whose "
    "own fabricated arithmetic didn't even match its own fabricated code. "
    "Acted on by graph_edge_route_after_check_output: triggers a retry with feedback that "
    "the tool was never actually called.",
)

agent_skipped_required_tool_total = Counter(
    "agent_skipped_required_tool_total",
    "Final answers where a skill was loaded this turn (use_skill) whose "
    "own text names run_command_in_sandbox as required, but that tool was "
    "never actually called, even though the final answer states a "
    "specific dollar figure (check_output's "
    "skipped_required_sandbox_after_skill). Real bug, found live: the "
    "deal-economics skill's own text says not to estimate this kind of "
    "number by hand, and the model estimated it by hand anyway — correct "
    "that one specific time, but nothing enforced it, and every other "
    "live freehand attempt at the same math landed on a wrong number. "
    "Acted on by graph_edge_route_after_check_output: triggers a retry with feedback to "
    "actually call the tool the skill named.",
)

agent_skipped_step_total = Counter(
    "agent_skipped_step_total",
    "Final answers given without a step they needed: the person asked for what one tool does (remember something, have a researcher look) and it was "
    "never called, or a loaded skill asked for a document search and the answer says nothing is on record without one "
    "(output_guardrails.skipped_step, docs/GRAPH_PATTERNS.md pattern 115). Sent back; replaced if it repeats.",
)

agent_system_prompt_leak_total = Counter(
    "agent_system_prompt_leak_total",
    "Final answers containing a long, verbatim run of the seeded system "
    "prompt's own text (check_output's reveals_system_prompt) — output-side "
    "defense-in-depth alongside app/core/moderation.py's input-side "
    "screening: an injection phrased in a way moderation's known-pattern "
    "regexes don't catch can still be caught here if it actually succeeds "
    "in getting the model to recite its instructions back. Acted on by "
    "graph_edge_route_after_check_output: triggers a retry, same as the other "
    "check_output-computed rejection reasons.",
)

agent_retry_exhausted_total = Counter(
    "agent_retry_exhausted_total",
    "Turns that gave up on retry_output's repair loop because the SAME "
    "check_output rejection reason (too_short/deferred/uncited/"
    "...) fired on MAX_CONSECUTIVE_SAME_RETRY_REASON consecutive "
    "rounds — the model wasn't converging, just repeating. Routes to "
    "retry_exhausted instead of another retry_output round: real bug, "
    "found live — a stuck deferral loop burned 6 full retry rounds and "
    "~18k tokens before graph_edge_route_after_agent's own (much blunter) "
    "MAX_TOKENS_PER_TURN cap finally cut it off, landing on the exact same "
    "outcome this metric's own routing now reaches in 2 rounds.",
)

agent_tool_budget_exceeded_total = Counter(
    "agent_tool_budget_exceeded_total",
    "Turns where the LLM requested more tool calls at once than MAX_TOOL_CALLS_PER_TURN allows",
)

agent_invalid_tool_call_total = Counter(
    "agent_invalid_tool_call_total",
    "Turns where the LLM emitted a tool_call whose name isn't a real registered tool "
    "(app/agent/graph/tool_guards.py's invalid_tool_call node) — a model output-quality issue, not dispatched "
    "or surfaced to human_approval",
)

agent_use_skill_without_search_total = Counter(
    "agent_use_skill_without_search_total",
    "Turns where the LLM called use_skill without ever calling skill_search "
    "first in the same turn (app/agent/graph/skill_guards.py's use_skill_without_search "
    "node, see use_skill_called_without_search) — rejected and looped back "
    "to agent instead of dispatched, so a fabricated skill name never gets "
    "as far as a 'not found' failure the model might narrate into the final "
    "answer. A rising rate here is a real signal the model is reaching for "
    "a packaged skill it shouldn't (SYSTEM_PROMPT's skill_search trigger "
    "over-firing on a plain 'how do I build X' question, say), not just a "
    "quality nitpick.",
)

agent_token_budget_exceeded_total = Counter(
    "agent_token_budget_exceeded_total",
    "Turns cut short by MAX_TOKENS_PER_TURN",
)

agent_context_retrieval_degraded_total = Counter(
    "agent_context_retrieval_degraded_total",
    "retrieve_context calls that failed and degraded to no pre-fetched context",
)

agent_history_compacted_total = Counter(
    "agent_history_compacted_total",
    "Turns where conversation history was trimmed after crossing "
    "HISTORY_TOKEN_CEILING, down to HISTORY_TOKEN_FLOOR",
)

agent_capability_gate_total = Counter(
    "agent_capability_gate_total",
    "Tool-call batches routed to human_approval because a tool's declared "
    "capability required it (mandatory), independent of require_approval (opt-in)",
    ["capability"],
)  # capability: mutating | outward

agent_checkpoint_issue_total = Counter(
    "agent_checkpoint_issue_total",
    "Resume attempts refused by resumability_error before Command(resume=...), and reclaimed turns "
    "that could not be continued (nothing left to run)",
    ["reason"],
)  # reason: checkpoint_lost | checkpoint_incompatible | nothing_to_continue (a reclaimed turn whose pending node is gone)

agent_missing_ctx_total = Counter(
    "agent_missing_ctx_total",
    "Turns rejected by reject_context: no valid SecurityCtx (tenant+principal) "
    "was stamped on the request before it reached the graph",
)

agent_unattended_pause_total = Counter(
    "agent_unattended_pause_total",
    "Pauses auto-declined by astream_events_turn_unattended (one count per "
    "decline round — a model that re-requests a declined write is declined "
    "again, up to UNATTENDED_MAX_DECLINE_ROUNDS) at "
    "a mandatory capability gate — its callers (app/adapters/inbound/workers/agent_worker.py's "
    "queue consumer, app/adapters/inbound/channels/telegram/bot.py) have no interactive human on "
    "the other end of the call to solicit a real decision from (unlike "
    "astream_events_turn's approval_required/astream_events_resume flow)",
)

agent_unattended_job_paused_total = Counter(
    "agent_unattended_job_paused_total",
    "An unattended job (a Telegram message) that reached its channel as approval_required instead of "
    "being declined by the worker, so the channel cancelled the pause itself "
    "(app/adapters/inbound/channels/telegram/bot.py::_decline_pause). The worker declines a pause for such a job "
    "(agent_unattended_pause_total counts those), so this should stay at zero: a count means a worker "
    "that predates the `unattended` flag ran the job, which is a rolling deploy that upgraded the "
    "channel before its workers (GRAPH_PATTERNS.md 89). Nothing ran and nothing is left paused, which "
    "is why it is a counter and not an alert",
)

channel_signin_total = Counter(
    "channel_signin_total",
    "A channel's sign-in at the authorization server (app/adapters/inbound/channels/oauth.py), by channel and outcome. The CLI and the Telegram channel are "
    "clients of the front door and hold a sign-in per person (specs/020)",
    ["channel", "outcome"],
)  # channel: cli | telegram; outcome: ok | refreshed | forbidden | unknown_person | credentials | unavailable

channel_door_refusal_total = Counter(
    "channel_door_refusal_total",
    "A request from the CLI or the Telegram channel that the front door (or the API behind it) did not serve (app/adapters/inbound/channels/door.py), by why. "
    "`unreachable` is the door not answering at all: the turn was NOT sent, and nothing falls back to publishing it directly",
    ["channel", "reason"],
)  # reason: unauthenticated | forbidden | not_found | rate_limited | unavailable | unreachable | invalid | other

channel_person_provisioned_total = Counter(
    "channel_person_provisioned_total",
    "Telegram people created at the authorization server by the bot itself (TELEGRAM_AUTO_PROVISION), by outcome",
    ["outcome"],
)  # outcome: created | failed

channel_telegram_update_total = Counter(
    "channel_telegram_update_total",
    "A Telegram update after it reached the bot's inbox (app/adapters/inbound/channels/telegram/inbox.py), by what became of it. `dropped` is an update the "
    "bot gave up on after a crash loop or could not read, and the person never got an answer",
    ["outcome"],
)  # outcome: handled | error | overflow | requeued | dropped

channel_telegram_approval_total = Counter(
    "channel_telegram_approval_total",
    "Approval buttons in the Telegram channel (specs/021), by what happened. `not_owner` and `expired` are taps that did nothing; `resume_failed` "
    "is a decision the door refused after the person tapped, which they are told about and can tap again",
    ["outcome"],
)  # outcome: shown | approved | rejected | expired | not_owner | duplicate | resume_failed | cancelled

channel_telegram_link_total = Counter(
    "channel_telegram_link_total",
    "Telegram account linking (specs/021), by step and outcome. `refused` covers an unknown, reused or expired state and a login the authorization "
    "server did not accept; the person is shown a plain page and nothing is bound",
    ["step", "outcome"],
)  # step: start | callback | confirm | refresh | unlink; outcome: ok | refused | unavailable | revoked

channel_telegram_webhook_total = Counter(
    "channel_telegram_webhook_total",
    "Requests to the bot's webhook (app/adapters/inbound/channels/telegram/server.py), by outcome. `forbidden` is a request without the secret Telegram was given",
    ["outcome"],
)  # outcome: accepted | forbidden | invalid | unavailable

agent_retrieval_degraded_total = Counter(
    "agent_retrieval_degraded_total",
    "Hybrid retrieval stages that failed and degraded (app/adapters/outbound/qdrant/qdrant_store.py::hybrid_search)",
    ["stage"],
)  # stage: sparse (-> dense-only) | rerank (-> RRF-fused order)

agent_semantic_cache_total = Counter(
    "agent_semantic_cache_total",
    "Semantic cache lookups by outcome (app/adapters/outbound/redis/semantic_cache.py)",
    ["outcome"],
)  # outcome: hit | miss | error (degraded — treated as a miss, recorded separately)

agent_embedding_identity_total = Counter(
    "agent_embedding_identity_total",
    "Embedding-identity checks of a vector collection (app/adapters/outbound/qdrant/qdrant_store.py::check_identity): did the vectors it holds come "
    "from the embedding model, dimension and prefixes this process is configured to use? A mismatch is REFUSED (reads degrade like a "
    "retrieval outage, writes fail), never answered with wrong-space results; see GRAPH_PATTERNS.md pattern 67.",
    ["outcome", "collection"],
)  # outcome: verified | adopted (legacy collection trusted, not provable) | mismatch (refused) | unverified (alias unresolvable, counted)

agent_model_route_total = Counter(
    "agent_model_route_total",
    "What happened to a turn's routing decision, one outcome per turn (app/agent/graph/model_route.py, app/agent/graph/agent_node.py; GRAPH_PATTERNS.md "
    "pattern 68; specs/015 contracts/tiered-router.md). `decided` is the normal case; every other outcome is a degradation or a step down, and the turn "
    "ran on the tier it says or lower.",
    ["outcome"],
)  # outcome: decided | disabled | no_text | label_none | label_timeout | label_error | label_unknown | tier_unpriced | error | routed_unreachable_fell_back (unreachable, a 5xx or out of quota)

agent_model_route_tier_total = Counter(
    "agent_model_route_tier_total",
    "The tier each routed turn was given, by mode and by which signal asked for it (app/agent/graph/model_route.py). In `shadow` mode this is the tier the "
    "turn WOULD have used and it ran on the local model; in `on` it is the tier used. Read the share per tier here before turning the router on.",
    ["tier", "mode", "decided_by"],
)  # tier: local | medium | strong; mode: shadow | on; decided_by: none | rules | label | both

agent_model_route_latency_seconds = Histogram(
    "agent_model_route_latency_seconds",
    "What the routing step added to a turn: the whole route_model node (the label round trip and the price check), in seconds",
    unit="s",
)

agent_ingest_total = Counter(
    "agent_ingest_total",
    "Successful ingest_text calls by source kind (app/ingestion/ingestor.py)",
    ["source"],
)  # source: text | file | url

agent_ingest_refused_total = Counter(
    "agent_ingest_refused_total",
    "Refused ingest attempts by reason (app/ingestion/ingestor.py)",
    ["reason"],
)  # reason: no_ctx | bad_file_type | ssrf_blocked | fetch_failed | too_large

agent_ingest_cleanup_failed_total = Counter(
    "agent_ingest_cleanup_failed_total",
    "Ingests that wrote a document's new version but could not remove its older one (app/ingestion/ingestor.py): both versions answer until the next ingest of that source "
    "(PRODUCTION_REVIEW.md R18)",
)

agent_checkpoint_sweep_deleted_total = Counter(
    "agent_checkpoint_sweep_deleted_total",
    "Conversations removed by scripts/jobs/checkpoint_sweep.py: their checkpoints and their session row (PRODUCTION_REVIEW.md R11)",
)

agent_feedback_total = Counter(
    "agent_feedback_total",
    "New ratings of an answer by the person who read it (app/agent/runtime/feedback.py); a rating that REPLACES the same person's earlier one is not counted again. "
    "Advisory (PRODUCTION_REVIEW.md R28)",
    ["rating"],
)  # rating: up | down

agent_answer_sample_total = Counter(
    "agent_answer_sample_total",
    "Conversations the daily sampling job read, by what became of each (scripts/jobs/answer_sample.py): judged (the answer had citations and was checked), "
    "no_citations (a general-knowledge or calculator answer: nothing to check), unreadable (no final answer in the saved state). Advisory (PRODUCTION_REVIEW.md R28)",
    ["outcome"],
)  # outcome: judged | no_citations | unreadable

agent_answer_sample_finding_total = Counter(
    "agent_answer_sample_finding_total",
    "Findings of the grounded-claims checks over the sampled answers: ungrounded_marker (a [n] no source offered), uncited (a source used without its marker), "
    "misattributed (a marker on a sentence unrelated to that source). Divide by agent_answer_sample_total{outcome=\"judged\"} for a rate. Heuristics tuned on small "
    "models: a trend, not a verdict (PRODUCTION_REVIEW.md R28)",
    ["finding"],
)  # finding: ungrounded_marker | uncited | misattributed

agent_db_pool_connection_lost_total = Counter(
    "agent_db_pool_connection_lost_total",
    "A database connection a caller was using turned out to be dead (PgBouncer or Postgres restarted): the pool then replaces all its dead connections at once "
    "(app/platform/self_healing_pool.py, PRODUCTION_REVIEW.md R25). A burst is a restart; a steady rate is a database that keeps dropping connections",
    ["pool"],
)  # pool: appdata | checkpointer

agent_moderation_total = Counter(
    "agent_moderation_total",
    "Input moderation screens by outcome (app/core/moderation.py)",
    ["outcome"],
)  # outcome: allowed | blocked_injection | blocked_denylist | blocked_ml_injection |
# error (degraded — treated as allowed)

agent_worker_job_reclaimed_total = Counter(
    "agent_worker_job_reclaimed_total",
    "Jobs reclaimed via XAUTOCLAIM after their original worker died mid-job "
    "(app/adapters/inbound/workers/streams.py::reclaim_stale_entries). 'retried' means the "
    "job was judged safe to run again (app/adapters/inbound/workers/agent_worker.py::"
    "_classify_reclaimed_turn — an unfinished turn is continued from its "
    "checkpoint, not restarted) and was silently republished; "
    "'dead_lettered' means it was surfaced as an error on its own results "
    "stream and archived instead (an already-finished or approval-paused "
    "turn, an unreadable checkpoint, or retries exhausted), since "
    "re-running it could have duplicated an already-applied side "
    "effect. Any sustained rate here means workers are crashing, not that "
    "recovery is working as intended — 'retried' vs 'dead_lettered' tells "
    "you whether that crashing is at least self-healing.",
    ["queue", "outcome"],
)  # queue: agent | ingest; outcome: retried | dead_lettered

agent_thread_lock_lost_total = Counter(
    "agent_thread_lock_lost_total",
    "A worker went to renew a conversation's lock while its job was still running and found it was no longer "
    "its own: it expired (a stalled event loop, a Redis that lost the key) and another job may now hold it. "
    "The turn is not stopped, so for the rest of it two jobs can write one conversation's checkpoints, the "
    "fork the lock exists to prevent (pattern 99). Always zero in a healthy deployment.",
)

agent_worker_last_loop_timestamp_seconds = Gauge(
    "agent_worker_last_loop_timestamp_seconds",
    "Unix time at which a queue worker's loop last passed (`queue`: agent | ingest; `loop`: read | reclaim), re-set on every pass. The same fact as "
    "the heartbeat file the container's health check reads (app/platform/worker_heartbeat.py). A worker whose event loop is stuck stays 'up' and "
    "stops updating this; alert AgentWorkerLoopStalled, which asks whether ANY replica of the queue has passed lately (WorkerUnreachable asks "
    "whether anyone took a job at all).",
    unit="s",
    labelnames=["queue", "loop"],
)  # queue: agent | ingest; loop: read | reclaim

agent_queue_wait_seconds = Histogram(
    "agent_queue_wait_seconds",
    "How long a job sat on a requests stream before a worker started it: the worker's `now` minus the `enqueued_at` the API stamped on the job, observed when "
    "the worker takes it (`queue`: agent | ingest). A job published by an older API during a rolling deploy carries no stamp and is skipped, and a "
    "reclaimed job keeps its ORIGINAL stamp, so a crash shows up here as a long wait. With agent_first_token_seconds and the queue gauges it is one of the "
    "numbers that size the worker pool (WORKER_CONCURRENCY.md). Alert AgentQueueWaitHigh. PRODUCTION_REVIEW.md R17.",
    unit="s",
    labelnames=["queue"],
)  # queue: agent | ingest

agent_queue_pending = Gauge(
    "agent_queue_pending",
    "Entries a requests stream's consumer group has delivered and not yet acknowledged: jobs running, or whose worker died. Re-set by every worker's reclaim "
    "pass from XINFO GROUPS (`domain`: a domain name for the chat queue, `ingest` for uploads). The collector forgets a series 5 minutes after its last "
    "update, which is why it is re-set every pass.",
    labelnames=["domain"],
)

agent_queue_lag = Gauge(
    "agent_queue_lag",
    "Entries on a requests stream that were added and no consumer has been given yet: jobs waiting for a worker. Read from XINFO GROUPS' `lag` in "
    "every worker's reclaim pass, and left unset when Redis cannot say (it reports none after entries were deleted from the middle of a group's "
    "unread range, which `ack_and_delete` does not do). Alert AgentQueueBacklog. A stream whose lag keeps rising means the pool is too small.",
    labelnames=["domain"],
)

agent_first_token_seconds = Histogram(
    "agent_first_token_seconds",
    "From the start of a turn's graph run to the first `token` event it sends: what a person waits before they see anything. It does NOT include the time the "
    "job spent on the queue (agent_queue_wait_seconds). A turn with no tokens at all (an error, an approval pause) observes nothing.",
    unit="s",
)

agent_worker_queue_read_failed_total = Counter(
    "agent_worker_queue_read_failed_total",
    "A blocking read of a Redis stream failed with a connection, timeout or socket error and was retried "
    "after a short backoff instead of ending the process (pattern 43): `agent` and `ingest` are the workers' "
    "XREADGROUP, `results` is the API's XREAD of a turn's events. A burst is a Redis restart or a network "
    "blip; a sustained rate is a Redis the workers cannot reach.",
    ["queue"],
)  # queue: agent | ingest | results

agent_tool_dedup_degraded_total = Counter(
    "agent_tool_dedup_degraded_total",
    "Mutating/outward tool calls where app/core/tool_idempotency.py::idempotent "
    "couldn't reach its own dedup store (a connection failure, or a failure "
    "to persist the result after a successful call) and fell back to "
    "running the tool unprotected instead — same degrade-don't-fail-the-turn "
    "posture as agent_moderation_ml_degraded_total. A sustained rate here "
    "narrows the window in which a reclaimed 'resume' retry could duplicate "
    "a side effect, since dedup wasn't actually available to catch it.",
)

agent_moderation_ml_degraded_total = Counter(
    "agent_moderation_ml_degraded_total",
    "Turns where the ML injection-classifier layer (app/core/moderation.py's "
    "call to ml-service's /prompt-guard) couldn't be reached and the turn "
    "proceeded on the pattern-based layer's result alone — same "
    "degrade-don't-fail-the-turn posture as agent_retrieval_degraded_total, "
    "counted separately from agent_moderation_total's own outcomes so a "
    "network blip against ml-service is distinguishable from a genuine "
    "'nothing flagged it' allow.",
)

agent_memory_deletion_total = Counter(
    "agent_memory_deletion_total",
    "Memory deletion calls by outcome (app/adapters/outbound/qdrant/memory.py)",
    ["outcome"],
)  # outcome: deleted | refused — never carries tenant/principal (see app/adapters/outbound/qdrant/memory.py's
# structured log line for the identified, per-call audit trail instead)

agent_no_progress_total = Counter(
    "agent_no_progress_total",
    "Turns ended early for repeating an identical tool-call batch "
    "MAX_REPEATED_ACTIONS times in a row (app/agent/graph/routing.py::graph_edge_route_after_agent)",
)

agent_cost_ceiling_exceeded_total = Counter(
    "agent_cost_ceiling_exceeded_total",
    "Turns ended early for exceeding MAX_COST_USD_PER_TURN (app/agent/graph/routing.py::graph_edge_route_after_agent)",
)

agent_cancellation_total = Counter(
    "agent_cancellation_total",
    "Paused runs cancelled via app/agent/runtime/stream.py::cancel_run (GRAPH_PATTERNS.md pattern 36)",
)

agent_streaming_cancellation_total = Counter(
    "agent_streaming_cancellation_total",
    "Actively-streaming (not paused) turns stopped via app/agent/runtime/core.py's cancel_check "
    "mechanism — the POST /chat/cancel queued-path counterpart to agent_cancellation_total, "
    "for a turn that wasn't paused at human_approval when the stop was requested",
)

agent_cancelled_turn_discarded_total = Counter(
    "agent_cancelled_turn_discarded_total",
    "What the thread got when a turn was stopped with POST /chat/cancel (app/agent/runtime/cancelled_turn.py, pattern 80): "
    "`dropped` = the whole turn was taken out so the next one cannot act on it, `closed` = a tool had already run, so the "
    "turn stays and ends with a line saying the person stopped it, `nothing` = no turn in the thread, `failed` = the "
    "thread could not be tidied and still holds the stopped message",
    ["outcome"],
)

agent_context_window_exceeded_total = Counter(
    "agent_context_window_exceeded_total",
    "Turns ended at the context_window_exceeded terminal node: the cumulative "
    "history_summary stayed over MAX_HISTORY_SUMMARY_CHARS even after "
    "compact_history just updated it (app/agent/graph/core.py::graph_edge_route_after_compact_history, "
    "GRAPH_PATTERNS.md pattern 41)",
)

agent_rate_limit_exceeded_total = Counter(
    "agent_rate_limit_exceeded_total",
    "Payment-webhook requests rejected by app/adapters/inbound/api/rate_limit.py's per-source-address limit (BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE). "
    "The per-tenant limiter that also counted here was removed in spec 018: turn limits are enforced at the front door, the authlimit service",
)

agent_budget_exceeded_total = Counter(
    "agent_budget_exceeded_total",
    "Turns refused before starting because a spend limit was already used up "
    "(app/agent/cost/budgets.py::check_allowance); only the limit that refused is counted. "
    "Replaces agent_tenant_budget_exceeded_total, which could not say whose limit it was.",
    ["scope", "window"],
)  # scope: tenant | principal ; window: day | month

agent_budget_threshold_total = Counter(
    "agent_budget_threshold_total",
    "Turns that were still allowed but had crossed a warning threshold of a spend limit — "
    "only the highest crossed (70, 85 or 95 percent) is counted. An early signal before "
    "agent_budget_exceeded_total starts firing. Replaces agent_tenant_budget_warning_total.",
    ["scope", "window", "threshold"],
)  # threshold: "70" | "85" | "95"

agent_gateway_budget_exceeded_total = Counter(
    "agent_gateway_budget_exceeded_total",
    "LLM calls the gateway refused because the app's key reached its max_budget "
    "(app/core/gateway.py). The gateway is the BACKSTOP: this firing means the app-level "
    "ceilings did not stop the spend first, or the backstop is sized too low.",
)

agent_gateway_content_blocked_total = Counter(
    "agent_gateway_content_blocked_total",
    "LLM calls the gateway refused because a content guardrail blocked them (app/core/gateway.py), "
    "answered with a refusal instead of an error. A policy working as configured, not a fault: "
    "a rise says people are asking for what the operator chose to refuse. `pre_call` blocked the "
    "request (the refused message is then replaced in the thread), `post_call`/`during_call` the "
    "response.",
    ["mode"],
)  # mode: pre_call | post_call | during_call | other

agent_tool_call_id_shortened_total = Counter(
    "agent_tool_call_id_shortened_total",
    "Tool calls whose id was too long to be a database key and was replaced by a digest of it. With a Gemini model this drops the thought "
    "signature LiteLLM carried in the id, and LiteLLM sends Google's placeholder instead.",
)

agent_unparsed_completion_total = Counter(
    "agent_unparsed_completion_total",
    "Model calls that reported output tokens but returned neither text nor a tool call: the provider "
    "generated something it could not hand back. Seen with Ollama (qwen2.5:3b): a `remember` tool call "
    "it could not parse was dropped, so the graph saw an empty reply (a `too_short` retry) and the "
    "write never happened. Nothing in the reply says it happened, so this counter is the only trace.",
)

agent_unpriced_usage_total = Counter(
    "agent_unpriced_usage_total",
    "LLM calls that spent tokens on a model with no known price (app/agent/cost/pricing.py) — "
    "every dollar ceiling reads $0 for that spend, so this must stay at zero in production",
    ["model_alias"],
)

agent_usage_reconcile_max_drift_usd = Gauge(
    "agent_usage_reconcile_max_drift_usd",
    "The largest per-tenant, per-day difference ABOVE tolerance between the usage events and the LLM gateway's spend log, in USD, "
    "as of the last reconciliation pass (0 when everything agrees). No tenant label: the report (`make usage-reconcile`) names them. "
    "Alert UsageReconcileDrift. The worker re-sets it every minute because the collector forgets a series 5 minutes after its last update.",
    unit="USD",
)

agent_usage_reconcile_total = Counter(
    "agent_usage_reconcile_total",
    "Gateway reconciliation passes by outcome: ok (everything agrees) | drift (something is above tolerance) | incomplete (the "
    "gateway had more rows than USAGE_RECONCILE_GATEWAY_MAX_PAGES, so that comparison was skipped) | failed (the pass "
    "itself raised: the gateway or database was unreachable; alert UsageReconcileNotCompleting).",
    ["outcome"],
)  # outcome: ok | drift | incomplete | failed

agent_cost_governance_degraded_total = Counter(
    "agent_cost_governance_degraded_total",
    "Cost-governance paths that failed and carried on instead of failing the turn "
    "(spec 008 A1)",
    ["path"],
)  # path: price_lookup | ledger_read | policy_read | reservation | model_resolve
# | usage_event_write | usage_event_table_missing | usage_event_identity | usage_missing (app/agent/cost/usage_events.py)
# (the billing package counts its own paths on its own instrument of this name: credit_billing/metrics.py)

agent_upload_rejected_total = Counter(
    "agent_upload_rejected_total",
    "POST /ingest/upload files rejected before any MinIO write",
    ["reason"],
)  # reason: bad_file_type | too_large | too_many_files

agent_upload_failed_total = Counter(
    "agent_upload_failed_total",
    "POST /ingest/upload files that failed AFTER an accepted upload attempt "
    "(MinIO write or job publish) — distinct from agent_upload_rejected_total, "
    "which is pre-write validation only",
    ["reason"],
)  # reason: storage_error

agent_worker_unreachable_total = Counter(
    "agent_worker_unreachable_total",
    "Queued jobs whose results stream received no event within the "
    "first-event deadline (app/adapters/outbound/redis/queue.py and app/adapters/outbound/redis/ingest_queue.py "
    "read_results) and, for chat, whose conversation no worker held — nobody picked the job up. One of these is a stuck request; a "
    "stream of them is a worker pool that answers nothing (none running for a "
    "domain, or all of them dead), which until this metric produced one error per "
    "caller and no signal an alert could use",
    ["queue"],
)  # queue: agent | ingest

agent_subagent_run_total = Counter(
    "agent_subagent_run_total",
    "run_subagent calls (app/agent/tools/builtin.py, GRAPH_PATTERNS.md pattern 46) by "
    "subagent and outcome",
    ["subagent", "outcome"],
)  # outcome: completed | budget_exceeded | timeout | error

agent_subagent_duration_seconds = Histogram(
    "agent_subagent_duration_seconds",
    "Wall-clock duration of one nested subagent run, in seconds",
    unit="s",
    labelnames=["subagent"],
)

agent_tool_retry_total = Counter(
    "agent_tool_retry_total",
    "Retry attempts made by app/core/resilience.py::CircuitBreaker.call after a "
    "transient (retry_on-listed) failure — counted per retry, not per call, so this "
    "can run well ahead of agent_circuit_breaker_opened_total under a brief blip "
    "every retry recovers from before the breaker ever trips.",
    ["dependency"],
)  # dependency: opensandbox_mcp | crawl4ai | serper

agent_circuit_breaker_opened_total = Counter(
    "agent_circuit_breaker_opened_total",
    "A CircuitBreaker (app/core/resilience.py) tripped open after failure_threshold "
    "consecutive calls to one dependency each exhausted their own retries — that "
    "dependency is presumed down, and every call to it fails fast with "
    "CircuitOpenError instead of retrying, until its cooldown elapses. A sustained "
    "rate here means a real outage of a shared local dependency (opensandbox-server, "
    "crawl4ai), not a per-tenant/per-request problem.",
    ["dependency"],
)

agent_circuit_breaker_rejected_total = Counter(
    "agent_circuit_breaker_rejected_total",
    "Calls rejected immediately with CircuitOpenError while a breaker was already "
    "open (including one already in CircuitState.HALF_OPEN, i.e. a trial call was "
    "already in flight) — each one is a call that would otherwise have paid the "
    "dependency's full connect-timeout for a failure the breaker already knows is "
    "coming, or piled a second concurrent probe onto a dependency still proving "
    "itself healthy.",
    ["dependency"],
)

agent_circuit_breaker_half_open_total = Counter(
    "agent_circuit_breaker_half_open_total",
    "A CircuitBreaker's cooldown elapsed and it admitted exactly one trial call "
    "(CircuitState.HALF_OPEN) to decide whether to fully close or re-open — every "
    "OTHER call arriving before that trial resolves is rejected instead of also "
    "being admitted (agent_circuit_breaker_rejected_total), so this dependency "
    "never gets hit with a burst the instant its cooldown expires.",
    ["dependency"],
)

agent_web_search_total = Counter(
    "agent_web_search_total",
    "One `web_search` call's outcome (app/adapters/outbound/web/serper.py), counted once per call that got as far as the client. ok / no_results "
    "are answers. not_configured: SERPER_API_KEY is empty, so no request was made. blank_query: refused locally, because "
    "serper.dev answers a blank query with unrelated results and spends a credit on it (measured). auth_failed (401/403; a wrong "
    "key is a measured 403) and rejected (any other 4xx; an account out of credits is expected to land here, which is NOT "
    "verified, as there is no safe way to exhaust one) need an operator: the feature is off until someone fixes the key or "
    "the account, and the turn does not fail, which is why they are alerted (WebSearchProviderRefusing). rate_limited (429), "
    "unavailable (5xx, a timeout, a dropped connection), circuit_open (the breaker is rejecting calls after repeated connection "
    "failures) and bad_response (200 but not the documented shape) are the provider or the network being unwell.",
    ["outcome"],
)  # outcome: ok | no_results | not_configured | blank_query | auth_failed | rejected | rate_limited | unavailable | circuit_open | bad_response

agent_mcp_tool_pin_failures_total = Counter(
    "agent_mcp_tool_pin_failures_total",
    "A remote MCP tool the caller pinned (app/adapters/outbound/mcp/client.py::load_remote_tools `pinned`: the tool's name plus a hash of its "
    "description and input schema) was left OUT of the catalog because the server now offers something else under that name "
    "(reason=changed) or no longer offers it (reason=missing). The hash covers exactly the text the model is shown, so a "
    "changed one is the signature of a dependency upgrade nobody reviewed, or of a server that rewrote its own tool "
    "descriptions after the pin was taken: the tool's description is where tool-poisoning instructions live, and the "
    "capability tier is local so it cannot be widened from there, but what the model is TOLD can. The feature behind the tool "
    "is unavailable until someone re-pins it on purpose.",
    ["dependency", "reason"],
)  # reason: changed | missing

agent_tool_output_truncated_total = Counter(
    "agent_tool_output_truncated_total",
    "A tool's text reached the model shorter than the tool produced it, because it exceeded MCP_TOOL_OUTPUT_MAX_CHARS "
    "(app/core/output_limits.py: head and tail kept, the middle replaced by a marker that says how much). Not an error and "
    "not alerted: a sandbox command that prints a megabyte is exactly what the cap exists for. A sustained rate on one "
    "dependency means the cap is too small for what that tool legitimately returns, or a tool is being asked for something "
    "it should be paginating.",
    ["dependency"],
)

agent_mcp_connection_events_total = Counter(
    "agent_mcp_connection_events_total",
    "Lifecycle of the long-lived stdio connection to a remote MCP server (app/adapters/outbound/mcp/connection.py): started (first process "
    "spawned on this event loop), restarted (a new one after the previous was lost), lost (found dead, or recycled after "
    "three timeouts in a row with no success between), closed (shut down on purpose). Before the connection was kept open "
    "a process was spawned per call, so this had no meaning; now `restarted` and `lost` are the signal that a bridge keeps "
    "dying. A call that was in flight when the server died is not replayed, so each `lost` can be one failed tool call.",
    ["dependency", "event"],
)  # event: started | restarted | lost | closed

agent_sandbox_unavailable_total = Counter(
    "agent_sandbox_unavailable_total",
    "The sandbox tool catalog could not be loaded (app/adapters/outbound/mcp/sandbox_tools.py::load_sandbox_tools): the feature degrades "
    "to 'no sandbox tools' and the turn goes on, which before this counter was a log line nobody was watching (specs/009 A9). "
    "reason=connection|timeout when the bridge started but could not be used, circuit_open when the breaker is refusing "
    "to try, other for anything else (opensandbox-mcp not installed, crashing at start, an SDK it cannot import).",
    ["reason"],
)  # reason: connection | timeout | circuit_open | other

agent_mcp_gateway_requests_total = Counter(
    "agent_mcp_gateway_requests_total",
    "What the MCP gateway (app/adapters/inbound/mcp/gateway.py) decided about the bearer token of a request, per published server: ok (a valid "
    "token for this server), invalid_token (bad signature, wrong or missing audience, expired, an algorithm outside the "
    "allow-list, an unknown key id), jwks_unavailable (the authorization server's signing keys could not be confirmed current, "
    "so NO token could be checked: to a client that looks like an expired login, so this label is the only place the outage "
    "shows), forbidden_tenant (a valid token whose tenant may not use this server). A request with no token, or without the "
    "required scope, is answered by the SDK before this code runs and is not counted here.",
    ["server", "outcome"],
)  # server: ecorp | ops; outcome: ok | invalid_token | jwks_unavailable | forbidden_tenant

agent_api_identity_total = Counter(
    "agent_api_identity_total",
    "What the API decided about the internal identity of a request in identity mode (app/platform/internal_identity.py, specs/018): ok, or "
    "the reason it was refused. missing (no identity header: the front door was bypassed or is not in front of this API), malformed, "
    "algorithm, type (the wrong `typ`), unknown_kid, signature, expired, not_yet_valid, issuer, audience, claims (no tenant or no "
    "subject), unexpected (the verifier itself failed, refused all the same), and keys_unavailable (the front door's public keys could "
    "not be confirmed current, so NO identity could be checked: the request is answered 503, not 401, and this label is the only place "
    "the outage shows). Never a token and never a claim.",
    ["outcome"],
)  # outcome: ok | missing | malformed | algorithm | type | unknown_kid | signature | expired | not_yet_valid | issuer | audience | claims | unexpected | keys_unavailable

agent_api_identity_key_refresh_total = Counter(
    "agent_api_identity_key_refresh_total",
    "Fetches of the front door's public signing keys (AUTHLIMIT_KEYS_URL) by the API: ok, or error (unreachable, a bad status, a body that "
    "is not a key set, or a key set with no usable key). An error keeps the last keys in use for AUTHLIMIT_KEYS_STALE_GRACE_SECONDS.",
    ["result"],
)  # result: ok | error

agent_api_auth_mode = Gauge(
    "agent_api_auth_mode",
    "Which way the API learns who is calling (AUTH_MODE, specs/018): 1 for the mode in force and 0 for the other, one series each. `identity` "
    "trusts only the signed identity the front door adds; `headers` trusts X-Tenant-Id and X-Principal-Id as any client sends them and must "
    "never face the internet: alert ApiHeaderAuthMode. The API re-sets it every minute (the collector forgets a series 5 minutes after its "
    "last update, so a value set once at startup would vanish).",
    labelnames=["mode"],
)  # mode: identity | headers

agent_team_channel_notify_total = Counter(
    "agent_team_channel_notify_total",
    "app/adapters/outbound/notify/team_channel.py::post_to_team_channel attempts by sink and outcome. "
    "This is a PIVOT transaction (can't be un-sent) backing a human_approval'd "
    "escalation/handoff/incident-log write that already committed — a failed "
    "send here used to be a log line only (no metric, so no alert could ever "
    "fire on it), meaning a human could go unnotified with nothing surfacing "
    "that fact beyond logs nobody was watching. Still best-effort (the write "
    "it follows is the real source of truth, pullable via list/status tools "
    "even if every push fails) — this only makes 'pushes have been failing' "
    "an observable, alertable fact instead of a silent one.",
    ["sink", "outcome"],
)  # sink: local | slack — outcome: ok | error

agent_profiling_start_failures_total = Counter(
    "agent_profiling_start_failures_total",
    "Times app/platform/profiling.py could not start the profiler although PYROSCOPE_SERVER_ADDRESS is set: sdk_missing (the pyroscope-io "
    "package is not installed) or configure_failed (the SDK raised). The process keeps running without profiles. No alert: a missing "
    "flame graph leaves no one unaware of committed business state. It cannot see a server that is down or not ready (the native sampler "
    "drops those pushes silently), so a gap in the Pyroscope UI with this at 0 is the server's side.",
    ["reason"],
)  # reason: sdk_missing | configure_failed


class MetricsCallbackHandler(BaseCallbackHandler):
    """Records tool-call/tool-error counts AND a structured per-call audit
    line (pattern 37) — tool name, a fingerprint of args/result (never raw
    content), and LangChain's run id as `tool_run_id` (`run_id` is the TURN's, on node lines) correlating start to outcome. Pass an
    instance in `config["callbacks"]` — fires for every tool run in the
    graph's ToolNode.

    Logged, not durably stored: the reference design this mirrors implies a
    synchronous durable-store write gating dispatch, more audit
    infrastructure than this demo has. Logging is the honest scope here.
    """

    def on_tool_start(self, serialized: dict[str, Any] | None, input_str: str, *, run_id: UUID, **kwargs: Any) -> None:
        name = (serialized or {}).get("name", "unknown")
        agent_tool_calls_total.labels(tool=name).inc()
        logger.info(
            "tool_called",
            extra={
                "tool": name,
                "tool_run_id": str(run_id),
                "args_fingerprint": _fingerprint(input_str),
            },
        )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        result_text = getattr(output, "content", output)
        logger.info(
            "tool_succeeded",
            extra={
                "tool_run_id": str(run_id),
                "result_fingerprint": _fingerprint(str(result_text)),
            },
        )

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        agent_tool_errors_total.inc()
        logger.warning(
            "tool_failed",
            extra={"tool_run_id": str(run_id), "error_class": type(error).__name__},
        )
