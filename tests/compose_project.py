"""Read a compose project the way compose does: an entry file and the parts it `include`s.

`deploy/dev/docker-compose.yml` and `deploy/prod/docker-compose.yml` hold no service, only a list of parts, and every path in them is relative to the repo root
(the entry's header says why). The tests that need "the services of the dev stack" or "every variable production reads" come here for that, and no further:
nothing runs compose, so the default suite stays hermetic.
"""
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} is not a mapping"
    return loaded


def parts(entry: Path) -> list[Path]:
    """The entry itself, then each file it `include`s in order (an overlay or a standalone project has none, so it is just itself)."""
    included = [item["path"] if isinstance(item, dict) else item for item in load(entry).get("include") or []]
    found = [REPO / path for path in included]
    missing = [str(path) for path in found if not path.is_file()]
    assert not missing, f"{entry.name} includes files that do not exist: {missing}"
    return [entry, *found]


def services(*entries: Path) -> dict[str, Any]:
    """Every service of the given entries and overlays, parts included, by name. A name defined twice would be a compose error, so the last wins here."""
    merged: dict[str, Any] = {}
    for entry in entries:
        for part in parts(entry):
            merged |= load(part).get("services") or {}
    return merged


def strings(node: Any) -> list[str]:
    """Every string in a parsed YAML document, keys included: where a `${VARIABLE}` can be (a comment cannot be)."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for key, value in node.items() for text in (*strings(key), *strings(value))]
    if isinstance(node, list):
        return [text for item in node for text in strings(item)]
    return []
