"""The `ModelCatalog` adapter (`app/ports/outbound/models.py`): what LiteLLM's `GET /model/info` admin endpoint says about the models behind the
gateway's aliases. It answers two questions for the application, the price list (`model_info`, read by `app/agent/cost/pricing.py`)
and the model an alias resolves to (`resolve`, read by the usage-event writer).

Resolve a chat model ALIAS (`config.CHAT_MODEL`, e.g. "chat") to the concrete model LiteLLM routes it to (e.g.
"ollama_chat/qwen2.5:3b") via LiteLLM's `GET /model/info` admin endpoint (pattern 38). The OpenAI-compatible
response body and LangChain's `response_metadata` echo only the alias; `x-litellm-model-name` carries the
resolved value per call, but `ChatOpenAI.invoke()` doesn't surface response headers, so this queries the
resolution directly.

Naming only aliases keeps the app portable (swapping providers is a config change) but lets the biggest lever
on output quality, a gateway remap, change with no recorded artifact. Resolving it here keeps model choice
invisible to routing but visible to forensics (the usage events, `app/agent/cost/usage_events.py`).

An answer is trusted for `MODEL_RESOLVE_TTL_SECONDS`, then asked for again. It used to be kept for the process life, so re-pointing the
`embed` alias at the gateway (a restart of LiteLLM alone) left every app process believing the old model: the embedding identity never
changed, and the new model's query vectors were searched against the old model's vectors (pattern 67). A refresh that fails keeps the last
answer, so a flapping proxy does not flip the identity.

Async, with a short negative cache. The first version was a synchronous `httpx.get` on the event loop at the
end of every completed turn: a slow LiteLLM froze every other turn, stream and health check on that worker for
the length of the lookup (measured: a 0.5 s answer stalled a concurrent heartbeat for 0.56 s), and while
LiteLLM was down EVERY recorded turn paid a fresh 5 s timeout for an answer that couldn't have changed (spec
008, B19). Now the request is awaited, and a lookup that failed or found no such alias isn't repeated for
`FAILED_LOOKUP_RETRY_SECONDS`.
"""
import logging
import time
from collections.abc import Mapping
from typing import Any, cast

import httpx

from app.core import metrics
from app.core.config import MODEL_RESOLVE_TTL_SECONDS, OPENAI_API_BASE, OPENAI_API_KEY
from app.ports.outbound.models import FAILED_LOOKUP_RETRY_SECONDS

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, str]] = {}  # alias -> (when it was answered, the model)

_failed_at: dict[str, float] = {}


def admin_base_url() -> str:
    """LiteLLM's admin endpoints (GET /model/info) live at the proxy
    root, not under the OpenAI-compatible /v1 prefix `OPENAI_API_BASE`
    already points at."""
    return OPENAI_API_BASE.removesuffix("/v1").removesuffix("/")


async def resolve_model(alias: str) -> str | None:
    """Best-effort lookup, cached for `MODEL_RESOLVE_TTL_SECONDS`. Never blocks
    or fails a turn: a failure (LiteLLM unreachable, alias not found, bad
    response shape) returns the last known answer, or `None` if there never
    was one. A failure is remembered for `FAILED_LOOKUP_RETRY_SECONDS` so a
    down proxy is asked about once per interval, not once per turn."""
    now = time.monotonic()
    cached = _cache.get(alias)
    last_known = cached[1] if cached is not None else None
    if cached is not None and now - cached[0] < MODEL_RESOLVE_TTL_SECONDS:
        return last_known
    failed_at = _failed_at.get(alias)
    if failed_at is not None and now - failed_at < FAILED_LOOKUP_RETRY_SECONDS:
        return last_known
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{admin_base_url()}/model/info",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            )
        response.raise_for_status()
        for entry in response.json().get("data", []):
            if entry.get("model_name") == alias:
                resolved = entry.get("litellm_params", {}).get("model")
                if isinstance(resolved, str) and resolved:
                    _cache[alias] = (time.monotonic(), resolved)
                    _failed_at.pop(alias, None)
                    return resolved
        _failed_at[alias] = time.monotonic()
        return last_known
    except Exception as exc:  # noqa: BLE001 - observability only, never fails a turn; counted instead
        _failed_at[alias] = time.monotonic()
        metrics.agent_cost_governance_degraded_total.labels(path="model_resolve").inc()
        logger.warning(
            "model resolution failed; continuing with the last known model, if any",
            extra={"alias": alias, "error_class": type(exc).__name__},
        )
        return last_known


async def model_info() -> list[Mapping[str, Any]]:
    """The gateway's `/model/info` entries. Raises on a transport or HTTP error: the caller (`pricing.get_price`) keeps serving the last good
    prices and counts the failure."""
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(
            f"{admin_base_url()}/model/info",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        )
    response.raise_for_status()
    return cast("list[Mapping[str, Any]]", response.json().get("data", []))


class LiteLLMModelCatalog:
    """The adapter: the module's functions behind the `ModelCatalog` port. Each call looks the function up at CALL time, so a test that
    patches `model_catalog.resolve_model` or `model_catalog.model_info` is seen."""

    async def model_info(self) -> list[Mapping[str, Any]]:
        return await model_info()

    async def resolve(self, alias: str) -> str | None:
        return await resolve_model(alias)
