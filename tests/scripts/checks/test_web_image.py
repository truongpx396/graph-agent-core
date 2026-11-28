"""`scripts/checks/web_image.py` runs against the built image (CI's `image` job): here, only that what is wrong is reported, not swallowed."""
from scripts.checks import web_image


def test_a_server_that_leaves_the_placeholder_and_serves_everything_is_reported(monkeypatch):
    page = '<meta name="agent-auth" content="__AGENT_AUTH__" />'
    monkeypatch.setattr(web_image, "_get", lambda url: (200, "public, immutable", page))

    found = "\n".join(web_image.problems("http://web", {"mode": "headers"}))

    for expected in ("the page is told", "placeholder", "names no bundle file", "a cached 404 would stay", "only the page and its bundle", "the proxy's 502"):
        assert expected in found, f"not reported: {expected}\n{found}"
