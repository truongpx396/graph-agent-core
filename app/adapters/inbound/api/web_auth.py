"""The built-in web page, server side (pattern 29; specs/023): where the built web client is, and what it is told about sign-in (specs/018).

The page is the React application in `web/`, built to static files (`make web-build`; the Dockerfile builds it into the image). `/` is its
`index.html` and `/static/assets/` its bundle, both public at the front door. The page needs to know whether the API is in `identity` mode
(a token is needed, the Tenant and Principal pickers are meaningless) or `headers` mode, and in identity mode where the authorization server
is. The smallest way to tell it is a <meta> tag the server writes into the page it serves: no extra request before the first, no route to
classify, no cache.

The tag carries no secret: the authorization server's address, the audience, the scope and the prefix of a tenant's client id. The value is
HTML-escaped, so a setting containing a quote can't break out of the attribute.
"""
import html
import json
from pathlib import Path
from typing import Any

from fastapi.staticfiles import StaticFiles

from app.core.config import settings

# A tenant's chat-API application at the authorization server has the client id `api-<tenant>` (scripts/tools/front_door_bootstrap.py creates it
# so), which is how the page finds the client of the organization a person types without being told every tenant's id in advance.
CLIENT_ID_PREFIX = "api-"
PLACEHOLDER = "__AGENT_AUTH__"
# The same relative place in a checkout and in the image (/app/web/dist).
WEB_DIST = Path(__file__).resolve().parents[4] / "web" / "dist"


class BuiltAssets(StaticFiles):
    """The bundle the page loads. An unbuilt checkout answers 404 per file; StaticFiles itself raises (a 500) when its directory is missing."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, check_dir=False)

    async def check_config(self) -> None:
        return None


def page_config() -> dict[str, Any]:
    """What the page is told. In header mode only the mode: nothing about an authorization server that is not in use."""
    if settings.auth_mode != "identity":
        return {"mode": "headers"}
    return {
        "mode": "identity",
        "issuer": settings.web_auth_issuer.strip().rstrip("/"),
        "audience": settings.web_auth_audience.strip().rstrip("/"),
        "scope": settings.web_auth_scope.strip(),
        "client_id_prefix": CLIENT_ID_PREFIX,
    }


def render(page: str) -> str:
    """The page with its placeholder replaced by the (escaped) configuration."""
    return page.replace(PLACEHOLDER, html.escape(json.dumps(page_config(), separators=(",", ":")), quote=True))
