"""Deleting an account and everything it made (P3.06, D-054), and web's retention purge.

The order matters. The account is disabled and signed out first, so nothing new can arrive. Core then
deletes its checks and has the gateway unlink its LLM calls. Only after that does the account row go,
with its sign-in attempts. Every step can run again, so a deletion that fails part-way (core or the
gateway down) is finished by the hourly purge, and the person is never left signed in to half an account.
"""

import logging

import httpx

from scc_web.auth import store
from scc_web.context import Deps
from scc_web.core_client import CoreError

log = logging.getLogger("scc.privacy")


async def delete_account(d: Deps, user_id: int, requested_by: int) -> bool:
    """Deletes the account. True when it is gone now; False when the purge will finish the deletion."""
    async with d.pool.connection() as conn:
        await store.request_deletion(conn, user_id, requested_by, d.clock())
    log.info(
        "account deletion requested",
        extra={"user_id": user_id, "requested_by": requested_by},
    )
    return await _finish(d, user_id, requested_by)


async def _finish(d: Deps, user_id: int, requested_by: int) -> bool:
    try:
        await d.core.erase_user(user_id, requested_by)
    except (CoreError, httpx.HTTPError) as exc:
        log.warning(
            "account deletion waits: core did not finish",
            extra={
                "user_id": user_id,
                "error": getattr(exc, "code", type(exc).__name__),
            },
        )
        return False
    async with d.pool.connection() as conn:
        await store.finish_deletion(conn, user_id)
    log.info("account deleted", extra={"user_id": user_id})
    return True


async def purge(d: Deps) -> dict[str, int]:
    """Old sign-in attempts and ended sessions, then any account deletion that hasn't finished."""
    async with d.pool.connection() as conn:
        counts = await store.purge(conn, d.clock(), d.config)
        pending = await store.pending_deletions(conn)
    finished = 0
    for user_id, requested_by in pending:
        finished += await _finish(d, user_id, requested_by)
    counts["deletions_finished"] = finished
    counts["deletions_waiting"] = len(pending) - finished
    log.info("purge finished", extra=counts)
    return counts
