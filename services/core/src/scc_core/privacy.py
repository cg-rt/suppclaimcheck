"""What core keeps about people, and for how long (P3.06, D-054, docs/PRIVACY.md).

A check holds what a person submitted: the text, the claims, the result, and (P4.03) their feedback.
Its events, passages, dropped quotes, injection events and feedback hang off the job row with
ON DELETE CASCADE, so deleting the job deletes all of it.

Admin actions on other people's checks (who reviewed or retried one) stay as a bare user ID: they
describe the admin's work, and once that admin's account is gone the number points at no one.
"""

from psycopg import AsyncConnection

# Final states of a Procrastinate task. A task still waiting or running is never deleted.
_FINISHED_TASKS = ["succeeded", "failed", "cancelled", "aborted"]


async def erase_user(conn: AsyncConnection, user_id: int) -> int:
    """Deletes every check the account made, with everything attached to it. Idempotent."""
    cur = await conn.execute("DELETE FROM core.jobs WHERE user_id = %s", (user_id,))
    # Injection events cascade with their job; this catches any whose job was already gone.
    await conn.execute(
        "DELETE FROM core.injection_events WHERE user_id = %s", (user_id,)
    )
    await conn.execute("DELETE FROM core.feedback WHERE user_id = %s", (user_id,))
    return cur.rowcount


async def purge(conn: AsyncConnection, config: dict) -> dict[str, int] | None:
    """Applies the retention periods in `config` (a pipeline config). None when it has no [retention]."""
    retention = config.get("retention")
    if retention is None:
        return None
    cur = await conn.execute(
        "DELETE FROM core.jobs WHERE created_at < now() - make_interval(days => %s)",
        (retention["check_days"],),
    )
    checks = cur.rowcount
    # Procrastinate's own clean-up query, plus: never a task the periodic schedule still points at.
    cur = await conn.execute(
        """
        DELETE FROM core.procrastinate_jobs WHERE id IN (
            SELECT job.id FROM (
                SELECT DISTINCT ON (j.id) j.id, j.status, e.at AS latest_at
                FROM core.procrastinate_jobs j
                JOIN core.procrastinate_events e ON e.job_id = j.id
                ORDER BY j.id, e.at DESC
            ) AS job
            WHERE job.status = ANY(%s::core.procrastinate_job_status[])
              AND job.latest_at < now() - make_interval(days => %s)
              AND NOT EXISTS (SELECT 1 FROM core.procrastinate_periodic_defers d WHERE d.job_id = job.id)
        )
        """,
        (_FINISHED_TASKS, retention["queue_days"]),
    )
    return {"checks_deleted": checks, "tasks_deleted": cur.rowcount}
