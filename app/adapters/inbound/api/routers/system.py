"""Liveness, readiness and the built-in web UI — the endpoints that need no caller identity."""
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import HTMLResponse

from app.adapters.inbound.api import (
    health as health_checks,  # `health` is also this module's own liveness endpoint name below
)
from app.adapters.inbound.api import web_auth
from app.adapters.inbound.api.schemas import HealthResponse, ReadinessResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse, tags=["web"])
def ui() -> str:
    """The built-in web UI (pattern 29): the web client in `web/`, built to
    static files, with no third-party script. It talks only to the published
    chat, session and ingest endpoints. 503 when the client has not been
    built (`make web-build`; the Docker image builds it). Read from disk per
    request (not cached), so a rebuild and a refresh is enough during
    development — this isn't a hot path.
    """
    try:
        page = (web_auth.WEB_DIST / "index.html").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail="The web client is not built. Run `make web-build` (needs Node 22); the Docker image builds it.",
        ) from exc
    return web_auth.render(page)


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    """Liveness only — always 200 if this process can respond at all. See
    `GET /health/ready` for whether it can actually complete a turn (see
    app/adapters/inbound/api/health.py on why these stay separate endpoints)."""
    return HealthResponse()


@router.get("/health/ready", response_model=ReadinessResponse, tags=["health"])
async def health_ready(response: Response) -> ReadinessResponse:
    """Readiness — 200 only if every real dependency is reachable right now,
    503 otherwise, with which one(s) failed in the body (see
    app/adapters/inbound/api/health.py::check_dependencies)."""
    checks = await health_checks.check_dependencies()
    ready = all(checks.values())
    response.status_code = 200 if ready else 503
    # Warnings never change the status or the code: an embedding-identity mismatch makes retrieval degrade, not the API unready.
    warnings = await health_checks.readiness_warnings()
    return ReadinessResponse(status="ready" if ready else "degraded", checks=checks, warnings=warnings)
