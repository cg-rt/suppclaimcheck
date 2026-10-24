"""Retries for outside calls (AGENTS.md section 7): exponential backoff with full jitter, transient errors only."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

log = logging.getLogger("scc.retry")


class Transient(Exception):
    """Raised by a call that may succeed if tried again: 429, 5xx, timeouts, dropped connections."""


async def with_retries[T](
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_s: float,
    max_s: float,
    what: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    """Runs `call`; on Transient waits a random time up to base * 2^n (capped) and tries again.

    Any other exception passes straight through: a 400 will not get better by repeating it.
    """
    rng = rng or random.Random()
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Transient as exc:
            if attempt == attempts:
                raise
            delay = rng.uniform(0, min(max_s, base_s * 2 ** (attempt - 1)))
            log.warning(
                "transient failure, retrying",
                extra={
                    "what": what,
                    "attempt": attempt,
                    "delay_s": round(delay, 2),
                    "error": str(exc)[:200],
                },
            )
            await sleep(delay)
    raise AssertionError("unreachable")
