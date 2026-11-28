"""The server-sent events the streaming routes send, as models for the OpenAPI document and for nothing else.

A stream is a sequence of `data: <one JSON object>` frames, and each object's `type` says which. A chat stream's frames also carry an `id:` (the position a client
sends back as `Last-Event-ID` to read on after a dropped connection) and the stream sends the SSE comment `: keepalive` while it is silent; an ingest stream's frames
have neither. No stream has an `event:` field. Standard SSE parsers ignore comments and fields they do not use. OpenAPI 3.1 has no way to say "a stream of these", so the document puts the schema of ONE event under `text/event-stream`, which is
what 3.2's `itemSchema` says properly; the route's description says so too.

A client ignores a `type` it does not know. A strict validator cannot, so a new member of the union is a major version (`make openapi-breaking`):
specs/025 declares what its phases need at once. For the same reason a vocabulary that may grow is a string with `x-extensible-enum`, not an enum.

Nothing builds an event through these models: events are plain dicts made in a hot loop (a `token` per token) in
app/agent/runtime/stream.py and the workers. What keeps a model from lying is the tests, which hold the events the real stream code and the
workers' own publishing make to the committed document (tests/agent/conftest.py, tests/adapters/inbound/workers/conftest.py).
"""
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


class EventStreamResponse(StreamingResponse):
    """For `response_class=` only, so the document lists a route's body as `text/event-stream`. FastAPI documents the body under the response
    class's media type, and StreamingResponse has none (it would say `application/json`)."""

    media_type = "text/event-stream"


# --- a chat turn: POST /chat/stream/queued, /chat/resume and /chat/cancel ---------------------------------------------------------


class Citation(BaseModel):
    marker: str = Field(description="`[1]`, `[2]`... as the answer cites it.")
    doc_id: str
    title: str
    text: str
    score: float
    source_url: str | None = Field(default=None, description="Where to read the original document (http or https). Absent when it has none, as an uploaded file does.")


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any]


class StatusEvent(BaseModel):
    """Not terminal. Where a turn's job is: `queued` the moment it is accepted, `started` when a worker takes it, before anything runs. A turn and a
    resume have both, `started` again if a crashed worker's job is run again; a cancel has neither. A turn without `started` is waiting for a worker."""

    type: Literal["status"]
    state: str = Field(description="`queued` or `started`. Ignore a value you do not know.", json_schema_extra={"x-extensible-enum": ["queued", "started"]})
    at: datetime
    worker: str | None = Field(default=None, description="On `started`: an opaque label of the worker that took the job.")


class UsageEvent(BaseModel):
    """Just before `done`: what this turn's model calls used, over any approval pause, subagents' included. Null means unknown, never zero."""

    type: Literal["usage"]
    model: str | None = Field(description="The model that wrote the answer. Null when no model did (a cached answer, a refused message).")
    route: str | None = Field(description="The tier that served it. Ignore a value you do not know.", json_schema_extra={"x-extensible-enum": ["local", "medium", "strong"]})
    input_tokens: int | None = Field(description="Of every call. Null when any call's tokens are unknown.")
    output_tokens: int | None
    cost_usd: float | None = Field(description="Of every call. Null when any call has no known price or reported no usage.")
    model_calls: int = Field(description="Calls counted. A delegated run that failed is not counted, and makes the other totals null.")


class SystemNoteEvent(BaseModel):
    """Not terminal. Something about the conversation rather than this answer: for example, an earlier approval pause that an app update made unresumable."""

    type: Literal["system_note"]
    content: str


class TokenEvent(BaseModel):
    """A piece of the answer. A `retry` event discards every token before it."""

    type: Literal["token"]
    content: str


# How much a call can do, as its domain declares it. Anything not read_only is approved by a person first (constitution Principle II).
Tier = Literal["read_only", "mutating", "outward"]


class ToolStartEvent(BaseModel):
    """A tool call is starting. `label` and `tier` are the server's: a client shows them and derives nothing from `tool` or `args`."""

    type: Literal["tool_start"]
    tool: str
    args: dict[str, Any]
    subagent: str | None = Field(default=None, description="Set when the call happened inside a subagent's nested run.")
    call_id: str | None = Field(default=None, description="Pairs this with its `tool_end`, also between parallel calls of one tool.")
    label: str | None = Field(default=None, description='What the tool does, for a person: "Create a support ticket".')
    tier: Tier | None = None
    at: datetime | None = None


class ToolEndEvent(BaseModel):
    type: Literal["tool_end"]
    tool: str
    subagent: str | None = None
    call_id: str | None = None
    outcome: str | None = Field(
        default=None,
        description="`ok`, `error` (it failed) or `refused` (it said no). A turn that recovers from either still ends normally.",
        json_schema_extra={"x-extensible-enum": ["ok", "error", "refused"]},
    )
    kind: str | None = Field(default=None, description="On `refused`: `blocked`, as an error's `details.kind`.")
    duration_ms: int | None = None
    summary: str | None = Field(default=None, description='One line about the result ("6 results"), when the tool has one to give.')


class ApprovalItem(BaseModel):
    """What one pending call would do, in words."""

    call_id: str
    tier: Tier
    title: str
    effect: str
    target: str | None = None
    visible_to: str | None = None
    reversible: bool = Field(description="False unless the call changes nothing or can be taken back.")


class RetryEvent(BaseModel):
    """Not terminal. Discard everything streamed so far: the answer was rejected or replaced, or the model's stream broke and was started again, and fresh tokens (or a corrected one) follow."""

    type: Literal["retry"]


class CompactedEvent(BaseModel):
    """Not terminal. Informational: older context was trimmed before the model ran."""

    type: Literal["compacted"]


class CitationsEvent(BaseModel):
    """Just before `done`, when the answer cited a source."""

    type: Literal["citations"]
    items: list[Citation]
    ungrounded_claims_count: int


class FollowupsEvent(BaseModel):
    """Just before `done`, when the agent has suggestions."""

    type: Literal["followups"]
    items: list[str]


class ApprovalRequiredEvent(BaseModel):
    """Terminal for this request: the turn is paused until `POST /chat/resume` approves or rejects the calls, all of them together."""

    type: Literal["approval_required"]
    tool_calls: list[ToolCall]
    approval_id: str | None = Field(default=None, description="The identity of this pause. Send it with the answer.")
    items: list[ApprovalItem] = Field(default_factory=list, description="One per tool call, in the same order.")


class DoneEvent(BaseModel):
    """Terminal: the turn finished."""

    type: Literal["done"]


class ErrorEvent(BaseModel):
    """Terminal. `code`, `message` and `details` are the canonical error envelope (app/core/errors.py): switch on `code`, or on `details.kind`
    (`blocked`: a rule said no; `failed`: something went wrong; `lost`: what was waited for or answered is gone; a cancel has none). Every error the API and the
    workers send carries it (a queued job nobody took is `worker_unreachable`, a resume with nothing to resume is `checkpoint_lost` or
    `checkpoint_incompatible`); the fields stay optional in the schema so a client written before that still validates."""

    type: Literal["error"]
    content: str = Field(description="A message for a person; also what a client without the envelope shows.")
    code: str | None = None
    message: str | None = None
    details: dict[str, Any] | None = None


ChatEvent = Annotated[
    StatusEvent
    | TokenEvent
    | ToolStartEvent
    | ToolEndEvent
    | RetryEvent
    | CompactedEvent
    | CitationsEvent
    | FollowupsEvent
    | SystemNoteEvent
    | UsageEvent
    | ApprovalRequiredEvent
    | DoneEvent
    | ErrorEvent,
    Field(discriminator="type"),
]


# --- an upload's progress: GET /ingest/stream/{job_id} -----------------------------------------------------------------------------


class IngestStartedEvent(BaseModel):
    type: Literal["started"]


class IngestProgressEvent(BaseModel):
    type: Literal["progress"]
    done: int
    total: int


class IngestDoneEvent(BaseModel):
    """Terminal."""

    type: Literal["done"]
    chunks: int


class IngestErrorEvent(BaseModel):
    """Terminal. Only a message: an upload's error is not the chat turn's envelope."""

    type: Literal["error"]
    content: str


IngestEvent = Annotated[IngestStartedEvent | IngestProgressEvent | IngestDoneEvent | IngestErrorEvent, Field(discriminator="type")]
