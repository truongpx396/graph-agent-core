"""Durable checkpoints for the compiled graph.

A paused `human_approval` gate is only a real safety control if it survives a redeploy, so the graph is compiled with a saver that persists
(`app/agent/runtime/core.py::init_graph_async`). LangGraph's `BaseCheckpointSaver` is the contract the graph speaks; this port is how the
runtime obtains one and releases what backs it, without naming Postgres or its connection pool (`app/adapters/outbound/postgres/checkpointing.py`).

Two properties belong to the contract, not to one adapter:
- `open` is called on the event loop that will drive the graph. A saver may bind a lock to the loop that created it (the Postgres one does:
  calling it from another loop raises "bound to a different event loop"), so every process opens it on its own serving loop.
- `open` prepares whatever the saver needs (its tables) and is safe to repeat on every process start, including each worker replica.
"""
from __future__ import annotations

from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.ports.slot import Slot


class Checkpointing(Protocol):
    async def open(self) -> BaseCheckpointSaver[Any]:
        """A durable saver bound to the calling loop, with its storage prepared."""
        ...

    async def close(self) -> None:
        """Releases what `open` acquired. A no-op when nothing was opened."""
        ...

    async def delete_thread(self, thread_id: str) -> None:
        """Removes every checkpoint and pending write of one conversation. Idempotent: a thread with none is not an error. Raises when `open` has not run in
        this process or the store cannot be reached. The caller decides that the conversation may go (retention, or its owner asking): this does not check
        who owns `thread_id` (PRODUCTION_REVIEW.md R11)."""
        ...


checkpointing: Slot[Checkpointing] = Slot("checkpointing", Checkpointing)
