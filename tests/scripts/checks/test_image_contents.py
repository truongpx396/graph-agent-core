"""`scripts/checks/image_contents.py` runs inside the built image (CI's `image` job): here, only that what is missing is reported, not swallowed."""
from app.adapters.inbound.api import web_auth
from scripts.checks import image_contents


def test_an_image_without_the_web_client_or_the_sandbox_bridge_is_reported(monkeypatch, tmp_path):
    async def no_tools():
        return [], None

    monkeypatch.setattr(web_auth, "WEB_DIST", tmp_path)
    monkeypatch.setattr(image_contents.sandbox_tools, "load_sandbox_tools", no_tools)
    found = "\n".join(image_contents.problems())
    assert "built web page" in found and "bundle is missing" in found and "sandbox bridge" in found
    assert "skill catalog" not in found and "subagent catalog" not in found, "the repo's own catalogs load"
