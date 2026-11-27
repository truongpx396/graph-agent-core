"""Where the router would send each of the web client's "Try asking" questions, against the tier each is marked with.

    make route-suggestions          (needs the ML service: it answers POST /v1/decide)

The same classifier call and the same `routing_policy.decide` the graph's `route_model` node makes, with every tier configured and no ceiling. It says
nothing about whether the router is on (ROUTER_MODE). Exits 1 on a mark that disagrees, or when the classifier could not be asked.
"""
import asyncio
import json
import sys
from pathlib import Path

from app.agent.graph.model_route import ROUTE_QUESTION
from app.bootstrap import close_adapters, wire
from app.core import routing_policy
from app.core.config import ROUTER_LABELS, ROUTER_LONG_PROMPT_CHARS
from app.ports.outbound.classification import route_classifier

SUGGESTIONS = Path(__file__).resolve().parents[2] / "web/src/features/workspace/suggestions.json"


async def check(suggestions: dict[str, list[dict[str, str]]]) -> int:
    """Prints one line per question and returns how many disagree with their mark or could not be judged."""
    wrong = 0
    for domain, entries in suggestions.items():
        for entry in entries:
            text, marked = entry["text"], entry.get("route", "local")
            labelled = await route_classifier.get().choose(ROUTE_QUESTION, text) if ROUTER_LABELS else None
            failed = labelled is not None and labelled.outcome in ("timeout", "error")
            label = labelled.label if labelled is not None and labelled.outcome == "labelled" else None
            tier = routing_policy.decide(text, label, ceiling="strong", configured=routing_policy.TIERS, long_prompt_chars=ROUTER_LONG_PROMPT_CHARS).tier
            wrong += int(failed or tier != marked)
            verdict = f"NO ANSWER ({labelled.outcome})" if failed and labelled else ("ok" if tier == marked else f"MARKED {marked}")
            print(f"{domain:8s} {tier:7s} {label or '-':11s} {verdict:20s} {text}")
    return wrong


async def main() -> int:
    wire()
    try:
        wrong = await check(json.loads(SUGGESTIONS.read_text(encoding="utf-8")))
    finally:
        await close_adapters()
    print(f"{wrong} to look at" if wrong else "every mark matches the router")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
