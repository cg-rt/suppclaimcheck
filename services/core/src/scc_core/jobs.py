"""Job storage (P1.05): create with an idempotency key, read, move between states, confirm.

Every function takes an open psycopg async connection; callers own transactions and pools.
"""

import hashlib
import json
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from scc_core.models import Claim, Job, JobCreate, JobEvent, JobState, JobSummary

_COLUMNS = (
    "id, user_id, state, input_type, input_text, extracted_claims, confirmed_claim, result, error, "
    "config_version, sensitive_topics, needs_review, created_at, updated_at"
)


class IdempotencyConflict(Exception):
    """The key was already used for a different request."""


class WrongState(Exception):
    def __init__(self, state: str):
        super().__init__(f"job is {state}")
        self.state = state


def request_hash(body: JobCreate) -> str:
    return hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True).encode()
    ).hexdigest()


async def create(
    conn: AsyncConnection,
    body: JobCreate,
    key: str,
    config_version: str,
    correlation_id: str | None,
) -> tuple[Job, bool]:
    """Returns the job and whether it was created now. The same key and body twice give the same job."""
    digest = request_hash(body)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            INSERT INTO core.jobs (user_id, idempotency_key, request_hash, input_type, input_text,
                                   config_version, correlation_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, idempotency_key) DO NOTHING
            RETURNING {_COLUMNS}
            """,
            (
                body.user_id,
                key,
                digest,
                body.input_type,
                body.input_text,
                config_version,
                correlation_id,
            ),
        )
        row = await cur.fetchone()
        if row is not None:
            await cur.execute(
                "INSERT INTO core.job_events (job_id, state) VALUES (%s, 'submitted')",
                (row["id"],),
            )
            return Job(**row), True
        await cur.execute(
            f"SELECT {_COLUMNS}, request_hash FROM core.jobs WHERE user_id = %s AND idempotency_key = %s",
            (body.user_id, key),
        )
        existing = await cur.fetchone()
    if existing["request_hash"] != digest:
        raise IdempotencyConflict(key)
    existing.pop("request_hash")
    return Job(**existing), False


async def get(conn: AsyncConnection, job_id: UUID) -> Job | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM core.jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    return Job(**row) if row else None


async def set_state(
    conn: AsyncConnection,
    job_id: UUID,
    state: JobState,
    detail: str | None = None,
    **fields,
) -> None:
    """Moves a job to a new state, optionally setting extracted_claims, result or error, and logs the event."""
    allowed = {
        "extracted_claims",
        "result",
        "error",
        "sensitive_topics",
        "needs_review",
    }
    if not set(fields) <= allowed:
        raise ValueError(f"unknown job fields: {set(fields) - allowed}")
    assignments = ["state = %s", "updated_at = now()"]
    values: list = [state.value]
    for name, value in fields.items():
        assignments.append(f"{name} = %s")
        plain = name in {"error", "sensitive_topics", "needs_review"}
        values.append(value if plain else Jsonb(value))
    await conn.execute(
        f"UPDATE core.jobs SET {', '.join(assignments)} WHERE id = %s",
        (*values, job_id),
    )
    await conn.execute(
        "INSERT INTO core.job_events (job_id, state, detail) VALUES (%s, %s, %s)",
        (job_id, state.value, detail),
    )


async def confirm(conn: AsyncConnection, job_id: UUID, claim: Claim) -> Job:
    """Stores the claim the user confirmed. Only a job waiting for confirmation can be confirmed."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            UPDATE core.jobs SET confirmed_claim = %s, state = 'searching', updated_at = now()
            WHERE id = %s AND state = 'awaiting_confirmation'
            RETURNING {_COLUMNS}
            """,
            (Jsonb(claim.model_dump()), job_id),
        )
        row = await cur.fetchone()
        if row is None:
            await cur.execute("SELECT state FROM core.jobs WHERE id = %s", (job_id,))
            current = await cur.fetchone()
            if current is None:
                raise LookupError(job_id)
            raise WrongState(current["state"])
        await cur.execute(
            "INSERT INTO core.job_events (job_id, state, detail) VALUES (%s, 'searching', 'confirmed')",
            (job_id,),
        )
    return Job(**row)


async def correlation_id(conn: AsyncConnection, job_id: UUID) -> str | None:
    cur = await conn.execute(
        "SELECT correlation_id FROM core.jobs WHERE id = %s", (job_id,)
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def list_for_user(
    conn: AsyncConnection, user_id: int, limit: int
) -> list[JobSummary]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, state, input_text, confirmed_claim, created_at,
                   result->>'verdict' AS verdict, (result->>'score')::int AS score
            FROM core.jobs WHERE user_id = %s ORDER BY created_at DESC LIMIT %s
            """,
            (user_id, limit),
        )
        return [JobSummary(**row) for row in await cur.fetchall()]


async def events(conn: AsyncConnection, job_id: UUID) -> list[JobEvent]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT state, detail, at FROM core.job_events WHERE job_id = %s ORDER BY id",
            (job_id,),
        )
        return [JobEvent(**row) for row in await cur.fetchall()]


async def flag_review(conn: AsyncConnection, job_id: UUID, topics: list[str]) -> None:
    """Marks a job for the owner's safety review (P3.03) without changing its state."""
    await conn.execute(
        "UPDATE core.jobs SET sensitive_topics = %s, needs_review = true, updated_at = now() WHERE id = %s",
        (topics, job_id),
    )


async def record_injection(
    conn: AsyncConnection,
    job_id: UUID,
    user_id: int,
    stage: str,
    source: str,
    config_version: str,
    window_hours: int,
) -> int:
    """Stores an injection event (P3.02) and returns how many of the user's checks had instructions in
    their own text inside the window, this one included."""
    await conn.execute(
        "INSERT INTO core.injection_events (job_id, user_id, stage, source, config_version)"
        " VALUES (%s, %s, %s, %s, %s)",
        (job_id, user_id, stage, source, config_version),
    )
    return await injection_checks(conn, user_id, window_hours)


async def injection_checks(
    conn: AsyncConnection, user_id: int, window_hours: int
) -> int:
    """The user's checks with instructions in their own submitted text inside the window."""
    cur = await conn.execute(
        "SELECT count(DISTINCT job_id) FROM core.injection_events"
        " WHERE user_id = %s AND source = 'submitted_text' AND at > now() - make_interval(hours => %s)",
        (user_id, window_hours),
    )
    return (await cur.fetchone())[0]


async def exists(conn: AsyncConnection, user_id: int, idempotency_key: str) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM core.jobs WHERE user_id = %s AND idempotency_key = %s",
        (user_id, idempotency_key),
    )
    return await cur.fetchone() is not None


async def fail_stalled(
    conn: AsyncConnection, states: list[str], minutes: int, code: str, message: str
) -> list[UUID]:
    """Moves every job stuck in one of `states` for more than `minutes` to failed, with an event that
    names `code`, in one guarded statement: a job that moved on meanwhile is left alone (P3.05)."""
    cur = await conn.execute(
        "UPDATE core.jobs SET state = 'failed', error = %s, updated_at = now()"
        " WHERE state = ANY(%s) AND updated_at < now() - make_interval(mins => %s)"
        " RETURNING id",
        (message, states, minutes),
    )
    ended = [row[0] for row in await cur.fetchall()]
    for job_id in ended:
        await conn.execute(
            "INSERT INTO core.job_events (job_id, state, detail) VALUES (%s, 'failed', %s)",
            (job_id, code),
        )
    return ended
