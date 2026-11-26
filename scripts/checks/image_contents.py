"""Run INSIDE the built app image (CI's `image` job): `docker run --rm --entrypoint python <image> -m scripts.checks.image_contents`.

A build proves the Dockerfile parses and the dependencies install; it says nothing about whether the image can DO anything. This image once built
green for weeks with no skills, no subagents and no sandbox bridge: all three degrade to "empty" instead of failing, so the API and workers booted
healthy and simply lacked the features. Running the image, as its own interpreter in its own working directory, is the only check that sees what landed.
"""
import asyncio
import importlib
import sys

from app.adapters.inbound.api import web_auth
from app.adapters.outbound.mcp import sandbox_tools
from app.agent.tools import skill_catalog, subagent_catalog
from app.bootstrap import wire


def problems() -> list[str]:
    found = []
    page = web_auth.WEB_DIST / "index.html"
    if not page.is_file() or web_auth.PLACEHOLDER not in page.read_text(encoding="utf-8"):
        found.append(f"the built web page is missing or has no sign-in placeholder ({page}): GET / would be a 503")
    if not list((web_auth.WEB_DIST / "assets").glob("*.js")):
        found.append("the web client's bundle is missing from the image")
    if not skill_catalog.load_skills():
        found.append("the skill catalog is empty (the skills/ folders under app/domains are missing from the image)")
    if not subagent_catalog.load_subagents():
        found.append("the subagent catalog is empty (the subagents/ folders under app/domains are missing from the image)")
    try:
        importlib.import_module("scripts.tools.index_skills")
    except ImportError as exc:
        found.append(f"the scripts package is missing or broken: {exc}")
    # The bridge's catalog is static, so the app's own loader can start it here with no sandbox server, and must find exactly the tools it
    # pins: the whole stdio path (mcp client, opensandbox-mcp server) in the built image.
    tools, _ = asyncio.run(sandbox_tools.load_sandbox_tools())
    if {tool.name for tool in tools} != set(sandbox_tools.PINNED_SANDBOX_TOOLS):
        found.append(f"the sandbox bridge did not list its pinned tools in the image (got {sorted(tool.name for tool in tools)})")
    return found


def main() -> int:
    wire()  # also proves every adapter the composition root names imports in the image
    found = problems()
    for problem in found:
        print(f"::error::{problem}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
