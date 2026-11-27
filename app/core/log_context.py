"""The ids every log line of one request or job carries (RUNTIME_PATTERNS.md 112): `request_id`, `thread_id`, `tenant`, `principal`,
`domain`, `job_id`.

An entry point opens a `scope` (the API's request middleware, a worker around a job); code that learns more later `add`s to it. Pure:
a `ContextVar`, no I/O; app/platform/logging_config.py renders it. Ids and names only, never message content or a credential.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_FIELDS: ContextVar[dict[str, str] | None] = ContextVar("log_context", default=None)


def _present(fields: dict[str, str | None]) -> dict[str, str]:
    return {name: str(value) for name, value in fields.items() if value}


def current() -> dict[str, str]:
    """A copy of what is bound now; empty outside any scope."""
    return dict(_FIELDS.get() or {})


@contextmanager
def scope(**fields: str | None) -> Iterator[None]:
    """The fields of one request or job, on top of any outer scope's; restored on exit, so a worker's next job inherits nothing."""
    token = _FIELDS.set({**current(), **_present(fields)})
    try:
        yield
    finally:
        _FIELDS.reset(token)


def add(**fields: str | None) -> None:
    """Add to the open scope IN PLACE. A new value set with `ContextVar.set` would be lost to the caller when this runs in a copied
    context (a middleware's task, a dependency in a thread); a shared dict is seen by the request's own access line. No-op outside a scope."""
    bound = _FIELDS.get()
    if bound is not None:
        bound.update(_present(fields))
