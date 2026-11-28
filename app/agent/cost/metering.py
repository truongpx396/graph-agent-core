"""The one place a chat-model call is made, priced and recorded.

A call site that must remember to meter will one day forget, so every site goes through `metered_invoke`,
making the meter the default and its absence something a test catches
(tests/agent/cost/test_metering_choke_point.py). For each call, in order:
  1. call the model with the tenant's identity attached for the gateway (`app/core/gateway.py`);
  2. price the reported usage once (`pricing.price_call`), keeping the token split and the rate;
  3. record one usage event (`app/agent/cost/usage_events.py`), best-effort and never raising.

That event is the whole record of the call: what the dollar caps sum (`spend.py`), the wallet is debited from,
a usage-billing provider would be sent, and the reconciliation checks against the gateway. There is no
per-turn ledger row any more (specs/010 T030c2).

A turn's safety budgets are deliberately NOT touched by a follow-up or compaction: compaction can summarise
thousands of tokens, and adding that to the turn's token budget could stop a perfectly good answer.

Returns the response plus `usage` and `cost_usd` (an unpriced call adds 0.0 to the running total), so the
agent node's per-turn ceiling and the event come from the same `PricedCall`.

The node that made a call also puts its `usage_entry` into `State.turn_usage`, which is what the turn's `usage` event is summed from (specs/025). There,
unlike in the running total, a number nobody knows stays None.
"""
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.agent.cost import pricing, usage_events
from app.core import gateway
from app.ports.outbound.llm import ChatModel

# A `State.turn_usage` entry for model use that happened and cannot be counted: it makes the turn's totals unknown, not smaller.
UNKNOWN_USAGE: dict[str, Any] = {"alias": None, "kind": "unknown", "input_tokens": None, "output_tokens": None, "cost_usd": None}

# Set around one tool call by the tool node; a delegated run that fails appends to it. A list and not a flag: the tool runs in a COPY of the context, so
# only a change to the same object reaches the node.
_lost: ContextVar[list[bool] | None] = ContextVar("lost_usage", default=None)


@contextmanager
def lost_usage() -> Iterator[list[bool]]:
    """Collects `note_lost_usage` calls made inside the block; non-empty means some model use will reach no `turn_usage` entry of its own."""
    lost: list[bool] = []
    token = _lost.set(lost)
    try:
        yield lost
    finally:
        _lost.reset(token)


def note_lost_usage() -> None:
    if (lost := _lost.get()) is not None:
        lost.append(True)


@dataclass(frozen=True)
class MeteredCall:
    response: Any
    usage: Mapping[str, Any]
    cost_usd: float
    priced: pricing.PricedCall
    model_alias: str
    kind: str

    @property
    def usage_entry(self) -> dict[str, Any]:
        """What `State.turn_usage` keeps of this call. A provider that reported no usage leaves the tokens and the cost unknown."""
        reported = self.usage.get("input_tokens") is not None
        return {
            "alias": self.model_alias,
            "kind": self.kind,
            "input_tokens": self.priced.input_tokens if reported else None,
            "output_tokens": self.priced.output_tokens if reported else None,
            "cost_usd": self.priced.cost_usd if reported else None,
        }


async def metered_invoke(llm: ChatModel, messages: Sequence[Any], *, config: Mapping[str, Any] | None, kind: str, model_alias: str, invoke_model: str | None = None) -> MeteredCall:
    """`config` is the LangGraph node config (None when a test calls a node directly, in which
    case no identity is sent and no event is written: there is no tenant to meter to).

    `invoke_model` is the gateway alias to call INSTEAD of the one `llm` was built with (a per-call `model=` keyword, which the chat client
    sends in place of its default). It is for the routing seam only (specs/014) and is passed together with `model_alias` set to the same
    value, so the call is priced and recorded as the model that served it. Left `None`, nothing is added to the call: the request is
    exactly what it was before this parameter existed."""
    override = {} if invoke_model is None else {"model": invoke_model}
    response = await llm.ainvoke(messages, **override, **gateway.identity_from_config(config))
    usage = getattr(response, "usage_metadata", None) or {}
    priced = await pricing.price_call(model_alias, usage)
    configurable = (config or {}).get("configurable") or {}
    ctx = configurable.get("ctx")
    thread_id = configurable.get("thread_id") or "unknown"
    await usage_events.record_call(
        ctx,
        thread_id=thread_id,
        message_id=getattr(response, "id", None),
        kind=kind,
        model_alias=model_alias,
        priced=priced,
    )
    return MeteredCall(response=response, usage=usage, cost_usd=priced.cost_usd or 0.0, priced=priced, model_alias=model_alias, kind=kind)
