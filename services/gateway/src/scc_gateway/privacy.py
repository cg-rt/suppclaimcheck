"""What the gateway keeps about people, and for how long (P3.06, D-054, docs/PRIVACY.md).

The gateway's only links to a person are `llm_calls.user_id` and the claim inside a stored answer
(`llm_calls.output`). Cost rows stay after both are gone: they are the budget's record, and without
the link they belong to no one.
"""

from psycopg import AsyncConnection


async def erase_user(conn: AsyncConnection, user_id: int) -> int:
    """Removes the account's link from its LLM calls and drops their stored answers. Idempotent.
    The ID is remembered, so a call that was still running for the account is cleaned by the next purge."""
    cur = await conn.execute(
        "UPDATE gateway.llm_calls SET user_id = NULL, output = NULL WHERE user_id = %s",
        (user_id,),
    )
    await conn.execute(
        "INSERT INTO gateway.erased_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (user_id,),
    )
    return cur.rowcount


async def purge(conn: AsyncConnection, config: dict) -> dict[str, int]:
    """Applies the retention periods in the active config. Returns how many rows each step changed."""
    counts = {}
    idempotency = config.get("idempotency")
    if idempotency:
        cur = await conn.execute(
            "UPDATE gateway.llm_calls SET output = NULL WHERE output IS NOT NULL"
            " AND created_at < now() - make_interval(days => %s)",
            (idempotency["output_retention_days"],),
        )
        counts["answers_cleared"] = cur.rowcount
    retention = config.get("retention")
    if retention:
        cur = await conn.execute(
            "UPDATE gateway.llm_calls SET user_id = NULL WHERE user_id IS NOT NULL"
            " AND created_at < now() - make_interval(days => %s)",
            (retention["user_link_days"],),
        )
        counts["links_removed"] = cur.rowcount
    cur = await conn.execute(
        "UPDATE gateway.llm_calls SET user_id = NULL, output = NULL"
        " WHERE user_id IN (SELECT user_id FROM gateway.erased_users)"
    )
    counts["late_calls_unlinked"] = cur.rowcount
    return counts
