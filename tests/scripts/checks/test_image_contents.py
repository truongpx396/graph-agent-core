"""`scripts/checks/image_contents.py` runs inside the built image (CI's `image` job): here, only that what is missing is reported, not swallowed."""
from scripts.checks import image_contents


def test_an_image_without_the_sandbox_bridge_is_reported(monkeypatch):
    async def no_tools():
        return [], None

    monkeypatch.setattr(image_contents.sandbox_tools, "load_sandbox_tools", no_tools)
    found = "\n".join(image_contents.problems())
    assert "sandbox bridge" in found
    assert "skill catalog" not in found and "subagent catalog" not in found, "the repo's own catalogs load"
