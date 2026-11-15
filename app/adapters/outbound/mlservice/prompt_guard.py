"""The `InjectionScorer` adapter (`app/ports/outbound/screening.py`): `POST /prompt-guard` on the `ml-service` container.

The loop's shared client (`mlservice/http.py`), so a turn's check reuses a connection instead of opening one. Adds about 25-100 ms per call; viable
because `ml-service` is already local and offline (no hosted API, no new model pull). Raises on any failure to reach it or to parse its answer.
"""
from app.adapters.outbound.mlservice.http import ml_client
from app.core.config import ML_SERVICE_URL

_ML_CHECK_TIMEOUT_SECONDS = 3


class MlServicePromptGuard:
    async def malicious_score(self, text: str) -> float:
        resp = await ml_client().post(f"{ML_SERVICE_URL}/prompt-guard", json={"texts": [text]}, timeout=_ML_CHECK_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return float(resp.json()[0]["malicious_score"])
