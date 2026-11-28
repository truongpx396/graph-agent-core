"""The web client's own container (web/Dockerfile, web/server/, RUNTIME_PATTERNS.md 116) agrees with the code on both sides of it.

What the image does when it runs is `scripts/checks/web_image.py`'s (CI's `image` job): it needs Docker. Here, hermetically: the sign-in settings the
page is told have two writers, and the paths the client requests have to be ones the container passes on.
"""
import html
import json
import re
from pathlib import Path

import pytest

from app.adapters.inbound.api import web_auth
from app.core.config import Settings, settings

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "web" / "server"
CASES = json.loads((SERVER / "testdata" / "agent-auth.json").read_text(encoding="utf-8"))
AUTH_FIELDS = ("auth_mode", "web_auth_issuer", "web_auth_audience", "web_auth_scope")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_api_tells_the_page_what_the_container_is_checked_to_tell_it(case, monkeypatch):
    for field in AUTH_FIELDS:
        monkeypatch.setattr(settings, field, case["env"].get(field.upper(), Settings.model_fields[field].default))

    assert web_auth.page_config() == case["page"]
    assert json.loads(html.unescape(web_auth.render(web_auth.PLACEHOLDER))) == case["page"]


def test_the_containers_template_reads_the_same_settings_and_client_rule():
    template = (SERVER / "agent-auth.tmpl").read_text(encoding="utf-8")

    assert set(re.findall(r'env "([A-Z_]+)"', template)) == {field.upper() for field in AUTH_FIELDS}
    assert f'"client_id_prefix" "{web_auth.CLIENT_ID_PREFIX}"' in template


def _requested_by_the_client() -> set[str]:
    """The API paths written in the client's sources, up to the first variable part."""
    sources = [path for path in (REPO / "web" / "src").rglob("*.ts*") if not path.name.endswith((".test.ts", ".test.tsx", ".gen.ts"))]
    return {found for path in sources for found in re.findall(r'["`](/[a-z][a-z_/-]*)', path.read_text(encoding="utf-8"))}


def test_the_container_passes_on_every_api_path_the_client_requests():
    matcher = re.search(r"^\s*@api path (.+)$", (SERVER / "Caddyfile").read_text(encoding="utf-8"), re.MULTILINE)
    assert matcher
    requested = _requested_by_the_client()
    assert {"/chat/sessions", "/ingest/upload"} <= requested, "the scan no longer finds the client's calls"

    def passed(path: str) -> bool:
        return any(path == rule or (rule.endswith("/*") and path.startswith(rule[:-1])) for rule in matcher.group(1).split())

    assert not [path for path in requested if not passed(path)], "a request the container answers with 404 instead of passing to the API"
