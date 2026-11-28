"""Does the built web image serve the page, configured from its environment, under the hardening compose applies? `python3 -m scripts.checks.web_image <image>`
(CI's `image` job; `make web-image-check`). The standard library and the Docker CLI only: CI runs it with the runner's own Python.

A build proves none of it: a template that renders nothing, a server the dropped capabilities stop from starting, or a renamed placeholder all build green.
The sign-in settings have two writers, the container's template (web/server/agent-auth.tmpl) and `web_auth.page_config` for `make serve`;
web/server/testdata/agent-auth.json is the one answer both are held to (the Python writer by tests/core/test_web_container.py).
"""
import html
import http.client
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CASES = Path(__file__).resolve().parents[2] / "web" / "server" / "testdata" / "agent-auth.json"
# What `x-hardened` gives the service in deploy/dev/app.yml and deploy/prod/app.yml.
HARDENING = ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--tmpfs=/tmp:size=16m")


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def _get(url: str) -> tuple[int, str, str]:
    """Status, Cache-Control and body. `http.client`, not `urlopen`: it speaks HTTP only, so no address can name a local file (semgrep's
    `dynamic-urllib-use` rule, which failed `main`'s security gate)."""
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=10)
    try:
        connection.request("GET", parts.path or "/")
        response = connection.getresponse()
        body = response.read()
        return response.status, response.headers.get("Cache-Control", ""), body.decode() if response.status < 300 else ""
    finally:
        connection.close()


def _wait(base: str) -> bool:
    for _ in range(40):
        try:
            if _get(f"{base}/healthz")[0] == 200:
                return True
        except OSError:  # not listening yet: refused, or reset by Docker's port forwarder
            pass
        time.sleep(0.25)
    return False


def problems(base: str, expected: dict[str, Any]) -> list[str]:
    status, _, page = _get(f"{base}/")
    meta = re.search(r'<meta name="agent-auth" content="([^"]*)"', page)
    if status != 200 or meta is None:
        return [f"GET / is {status}, and has {'no' if meta is None else 'a'} sign-in tag"]
    found = []
    try:
        told = json.loads(html.unescape(meta.group(1)))
    except json.JSONDecodeError:
        told = meta.group(1)
    if told != expected:
        found.append(f"the page is told {told!r}, expected {expected!r}")
    if "{{" in page or "__AGENT_AUTH__" in page:
        found.append("the page still holds the placeholder or a template")
    bundle = re.findall(r'(?:src|href)="(/static/assets/[^"]+)"', page)
    if not bundle:
        found.append("the page names no bundle file")
    for path in bundle:
        status, cache, _ = _get(base + path)
        if status != 200 or "immutable" not in cache:
            found.append(f"GET {path} is {status} with Cache-Control {cache!r}")
    status, cache, _ = _get(f"{base}/static/assets/missing.js")
    if status != 404 or "immutable" in cache:
        found.append(f"a missing bundle file is {status} with Cache-Control {cache!r}: a cached 404 would stay")
    for path in ("/agent-auth.tmpl", "/index.html", "/static/index.html"):
        if (status := _get(base + path)[0]) != 404:
            found.append(f"GET {path} is {status}: only the page and its bundle are served")
    # No API is running beside this container: a 502 shows the path is passed on, not answered here.
    if (status := _get(f"{base}/chat/sessions")[0]) != 502:
        found.append(f"GET /chat/sessions is {status}, expected the proxy's 502")
    return found


def check(image: str, name: str, env: dict[str, str], expected: dict[str, Any]) -> list[str]:
    flags = [flag for key, value in env.items() for flag in ("-e", f"{key}={value}")]
    container = _docker("run", "-d", *HARDENING, "-p", "127.0.0.1::8080", *flags, image)
    try:
        address = _docker("port", container, "8080/tcp").splitlines()[0] if _docker("inspect", "-f", "{{.State.Running}}", container) == "true" else ""
        found = problems(f"http://{address}", expected) if address and _wait(f"http://{address}") else ["the server did not start"]
        if found:
            logs = subprocess.run(["docker", "logs", "--tail", "20", container], capture_output=True, text=True, check=False)
            print(logs.stdout + logs.stderr)
        return [f"{name}: {problem}" for problem in found]
    finally:
        _docker("rm", "-f", container)


def main(image: str) -> int:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    found = [problem for case in cases for problem in check(image, case["name"], case["env"], case["page"])]
    for problem in found:
        print(f"::error::{problem}")
    print(f"{len(cases)} configurations checked, {len(found)} problems")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
