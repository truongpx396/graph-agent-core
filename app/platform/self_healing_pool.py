"""A connection pool that replaces its dead connections after the first one fails (PRODUCTION_REVIEW.md R25).

After PgBouncer or Postgres restarts, every connection the pool holds is dead, and `psycopg_pool` only finds out one connection at a time, by handing each to a caller whose first
query then fails: a pool of 18 produced up to 18 failed requests. The work order proposed `check=AsyncConnectionPool.check_connection`, which tests a connection as it is handed
out. Measured against a real Postgres whose connections were all killed, with psycopg_pool 3.3.1, that is WORSE: the check loop discards the dead connections one at a time and
waits a backoff that DOUBLES between them (1, 2, 4, 8, 16 s), so with five or more dead connections it spends longer than the pool's 30 s timeout while replacements sit ready,
and the first request fails with `PoolTimeout` after 30 s. (It also makes every checkout about 25 times dearer, 6 to 150 microseconds, which on its own would be fine.)

What works is the pool's own `check()`, run once, in the background, when a connection a caller was using turns out to be broken: it tests every idle connection and replaces
the dead ones in one pass. Measured the same way: 2 failed requests at any pool size (the one that found out, and one that raced the check), none slow, against 6 of 10 and
11 of 22 for the plain pool.

It acts only on a connection that is actually broken (`conn.broken`), so an ordinary query failure (a deadlock, a cancelled statement, a constraint) never triggers it, and a
`PoolTimeout` (the pool is exhausted, not dead) does not either. At most one check runs at a time. It re-raises exactly what it caught: this changes what happens NEXT, never
what the failing caller sees.
"""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.core import metrics

logger = logging.getLogger(__name__)


class SelfHealingPool(AsyncConnectionPool):
    _healing: "asyncio.Task[None] | None" = None

    @contextlib.asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncIterator[Any]:
        held: Any = None
        try:
            async with super().connection(timeout) as conn:
                held = conn
                yield conn
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            if held is not None and not isinstance(exc, PoolTimeout) and getattr(held, "broken", False):
                self._heal()
            raise

    def _heal(self) -> None:
        """Starts the one background check, unless one is already running."""
        metrics.agent_db_pool_connection_lost_total.labels(pool=self.name or "unnamed").inc()
        if self._healing is not None and not self._healing.done():
            return
        logger.warning("db_pool_connection_lost", extra={"pool": self.name})
        self._healing = asyncio.ensure_future(self._check_all())

    async def _check_all(self) -> None:
        try:
            await self.check()
        except Exception as exc:  # noqa: BLE001 - a failed background check is only the status quo: the next failures will start another
            logger.warning("db_pool_check_failed", extra={"pool": self.name, "error_class": type(exc).__name__})
