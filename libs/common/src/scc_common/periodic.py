"""Work that runs on a timer inside a module's own process (P3.06): the retention purge in web and
the gateway. Core uses Procrastinate's periodic tasks instead, because it already has a worker.

Each run holds a Postgres advisory lock for its duration, so two copies of a module never run the
same work at once. A run that fails is logged; the next one tries again. The work is idempotent.
"""

import asyncio
import contextlib
import logging
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("scc.periodic")

Work = Callable[[], Awaitable[None]]


def lock_key(name: str) -> int:
    """A stable advisory-lock number for a task name."""
    return zlib.crc32(name.encode())


async def run_once(pool: AsyncConnectionPool, name: str, work: Work) -> bool:
    """Runs `work` while holding the task's lock. False when another process holds it (nothing ran)."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (lock_key(name),)
        )
        if not (await cur.fetchone())[0]:
            return False
        await work()
    return True


async def _loop(
    pool: AsyncConnectionPool, name: str, work: Work, seconds: float, first_delay: float
) -> None:
    await asyncio.sleep(first_delay)
    while True:
        try:
            await run_once(pool, name, work)
        except Exception:
            log.exception("periodic task failed", extra={"task": name})
        await asyncio.sleep(seconds)


@contextlib.asynccontextmanager
async def every(
    pool: AsyncConnectionPool,
    name: str,
    work: Work,
    seconds: float,
    first_delay: float = 60.0,
) -> AsyncIterator[asyncio.Task]:
    """Runs `work` every `seconds` (the first time after `first_delay`) for as long as the block is open."""
    task = asyncio.create_task(_loop(pool, name, work, seconds, first_delay))
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
