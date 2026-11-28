"""The `usage` event of a finished turn (specs/025): what `State.turn_usage` adds up to. A total with any unknown part is None, never smaller."""
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from app.agent.graph.model_route import tier_of
from app.ports.outbound import models

logger = logging.getLogger(__name__)


def _total(entries: Sequence[Mapping[str, Any]], key: str, zero: float) -> float | None:
    parts = [entry[key] for entry in entries]
    return None if any(part is None for part in parts) else sum(parts, zero)


async def _model_name(alias: str) -> str:
    """The model behind a gateway alias, as the usage events record it; the alias itself when the gateway cannot say."""
    try:
        return await models.model_catalog.get().resolve(alias) or alias
    except Exception as exc:  # noqa: BLE001 - a name for a footer: the turn is finished and its answer must not wait on or fail over it
        logger.warning("usage_model_name_unavailable", extra={"error_class": type(exc).__name__})
        return alias


async def usage_event(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """`model` and `route` are those of the call that wrote the answer (the main agent's last); the totals are of every call, delegated ones included."""
    answer = next((entry for entry in reversed(entries) if entry["kind"] == "chat"), None)
    return {
        "type": "usage",
        "model": await _model_name(answer["alias"]) if answer else None,
        "route": tier_of(answer["alias"]) if answer else None,
        "input_tokens": _total(entries, "input_tokens", 0),
        "output_tokens": _total(entries, "output_tokens", 0),
        "cost_usd": _total(entries, "cost_usd", 0.0),
        "model_calls": sum(entry["alias"] is not None for entry in entries),
    }
