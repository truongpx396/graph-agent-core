"""The log lines as a process writes them: through the production formatter, so the bound ids and the scrubbing are in them (`caplog`
holds the records before either)."""
import io
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from app.platform.logging_config import build_formatter


@contextmanager
def json_logs(level: int = logging.INFO) -> Iterator[Callable[[], list[dict[str, Any]]]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_formatter())
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
