"""The built-in web page, server side (pattern 29; specs/023, specs/018): `/` is the web client's built `index.html` with the sign-in mode
written into it, and `/static/assets/` its bundle.

Hermetic and without Node: a stand-in build (the real source `web/index.html`, which Vite keeps the <meta> tag of, plus one asset) takes the
place of `web/dist`. What the client does with the tag, sign-in included, is tested where it runs (`web/src/features/auth/auth.test.ts`,
CI's `web` job). The real proof, a person signing in with a real browser against a real Casdoor, is `scripts/checks/authlimit_web_check.py`,
a manual tier.
"""
import html
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from app.adapters.inbound.api import main as api
from app.adapters.inbound.api import web_auth
from app.adapters.inbound.api.routers.system import ui
from app.core.config import settings

REPO = Path(__file__).resolve().parents[4]
SOURCE_PAGE = (REPO / "web" / "index.html").read_text(encoding="utf-8")
ROUTES = json.loads((REPO / "services" / "authlimit" / "routes.json").read_text(encoding="utf-8"))["routes"]


@pytest.fixture
def built(tmp_path, monkeypatch) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(SOURCE_PAGE, encoding="utf-8")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not an asset", encoding="utf-8")
    monkeypatch.setattr(web_auth, "WEB_DIST", tmp_path)
    return tmp_path


def _meta(page: str) -> dict:
    (content,) = re.findall(r'<meta name="agent-auth" content="([^"]*)"\s*/?>', page)
    return json.loads(html.unescape(content))


def _assets(directory: Path) -> TestClient:
    return TestClient(Starlette(routes=[Mount("/static/assets", web_auth.BuiltAssets(directory))]))


def test_header_mode_tells_the_page_the_mode_and_nothing_about_an_authorization_server(built):
    assert _meta(ui()) == {"mode": "headers"}


def test_identity_mode_tells_the_page_where_to_sign_in_what_to_ask_for_and_how_to_find_the_tenants_client(built, identity_mode, monkeypatch):
    monkeypatch.setattr(settings, "web_auth_issuer", "https://auth.example.test/")
    monkeypatch.setattr(settings, "web_auth_audience", "https://api.example.test/")

    assert _meta(ui()) == {
        "mode": "identity",
        "issuer": "https://auth.example.test",
        "audience": "https://api.example.test",
        "scope": "api:chat",
        "client_id_prefix": "api-",
    }, "trailing slashes are removed: the audience must equal the front door's byte for byte"


def test_a_setting_cannot_break_out_of_the_attribute_it_is_written_into(built, identity_mode, monkeypatch):
    monkeypatch.setattr(settings, "web_auth_issuer", 'https://x.test"><script>alert(1)</script><meta x="')

    page = ui()

    assert "<script>alert(1)" not in page
    assert _meta(page)["issuer"].startswith("https://x.test\"><script>"), "the value survives intact, as data"


def test_the_placeholder_is_always_replaced(built):
    assert web_auth.PLACEHOLDER in SOURCE_PAGE and web_auth.PLACEHOLDER not in ui()


def test_an_unbuilt_client_is_a_503_that_says_how_to_build_it(tmp_path, monkeypatch):
    monkeypatch.setattr(web_auth, "WEB_DIST", tmp_path / "nothing-here")

    with pytest.raises(HTTPException) as refused:
        ui()

    assert refused.value.status_code == 503 and "make web-build" in refused.value.detail


def test_the_bundle_is_served_and_nothing_outside_it_is(built):
    client = _assets(built / "assets")

    script = client.get("/static/assets/index-abc123.js")

    assert script.status_code == 200 and "javascript" in script.headers["content-type"] and script.text == "console.log(1)"
    assert client.get("/static/assets/..%2fsecret.txt").status_code == 404
    assert client.get("/static/assets/%2e%2e/index.html").status_code == 404


def test_an_unbuilt_bundle_is_a_404_not_a_server_error(tmp_path):
    assert _assets(tmp_path / "nothing-here").get("/static/assets/index.js").status_code == 404


def test_the_app_serves_the_bundle_where_the_page_asks_for_it_and_it_is_not_part_of_the_api_document():
    (mount,) = [route for route in api.app.routes if getattr(route, "name", "") == "web-assets"]

    assert mount.path == "/static/assets" and isinstance(mount.app, web_auth.BuiltAssets)
    assert Path(mount.app.directory) == REPO / "web" / "dist" / "assets", "the checkout and the image keep it at the same relative place"
    assert not [path for path in api.app.openapi()["paths"] if path.startswith("/static")]
    # Vite is told the same prefix: a build with another base would load nothing.
    assert '"/static/"' in (REPO / "web" / "vite.config.ts").read_text(encoding="utf-8")


def test_the_front_door_lets_the_page_and_its_bundle_through_unchecked_because_a_person_must_load_them_before_signing_in():
    for pattern in ("/", "/static/*"):
        rule = next(r for r in ROUTES if r["method"] in ("*", "GET") and r["pattern"] == pattern)
        assert rule["class"] == "public", pattern
