"""core-api admin endpoints (P3.08, P3.09): jobs by state, per-job traces, recent errors, the queue,
and a manual retry for failed jobs.

Internal only, for web's admin pages: web may read only its own `auth` schema (D-011), so core data
reaches admin through here. Everything is read-only except the retry.

The retry is deliberately simple: a job in `failed` goes back to the start of the phase that failed
(extraction before confirmation, the search after it) on the config version it was created under,
and the matching Procrastinate task is queued again. The state change is guarded, so two clicks
queue one task. Idempotency keys on internal calls arrive with P3.05.
"""

import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from scc_common import correlation
from scc_core import tasks

log = logging.getLogger("scc.admin")
router = APIRouter(prefix="/admin", tags=["admin"])

# Jobs by state as admin groups them. Any state not named here is "in progress", so states added
# later (P3.01's triage, for example) still show up somewhere.
SETTLED = {
    "done": ("done",),
    "awaiting_confirmation": ("awaiting_confirmation",),
    "failed": ("failed",),
    "refused": ("refused",),
}
STATUSES = ("in_progress", *SETTLED)
Status = Literal["in_progress", "done", "awaiting_confirmation", "failed", "refused"]
_SETTLED_STATES = [s for states in SETTLED.values() for s in states]
# A job in progress whose state hasn't changed for this long has probably stalled (worker crash).
STALLED_MINUTES = 15

_STATUS_SQL = (
    "CASE "
    + " ".join(
        f"WHEN j.state IN ({', '.join(repr(s) for s in states)}) THEN '{name}'"
        for name, states in SETTLED.items()
    )
    + " ELSE 'in_progress' END"
)
_FAILURE_CODE_SQL = (
    "CASE WHEN j.state = 'failed' THEN (SELECT e.detail FROM core.job_events e"
    " WHERE e.job_id = j.id AND e.state = 'failed' ORDER BY e.id DESC LIMIT 1) END"
)


class AdminJobRow(BaseModel):
    id: UUID
    user_id: int
    state: str
    status: Status
    input_preview: str
    supplement: str | None
    effect: str | None
    verdict: str | None
    score: int | None
    failure_code: str | None
    config_version: str
    correlation_id: str | None
    created_at: datetime
    updated_at: datetime


class AdminJobList(BaseModel):
    """One page of jobs, newest first, and how many jobs match the search in each status."""

    counts: dict[str, int]
    jobs: list[AdminJobRow]
    more: bool


class AdminJob(BaseModel):
    id: UUID
    user_id: int
    state: str
    input_type: str
    input_text: str
    extracted_claims: list[dict] | None
    confirmed_claim: dict | None
    result: dict | None
    error: str | None
    config_version: str
    correlation_id: str | None
    created_at: datetime
    updated_at: datetime


class TraceEvent(BaseModel):
    state: str
    detail: str | None
    at: datetime
    # Until the next event, or until now for the job's current state; None once the job is settled.
    seconds: float | None


class ArticlePassages(BaseModel):
    pmcid: str
    passages: int


class DroppedQuote(BaseModel):
    stage: str
    passage_ref: str | None
    quote: str
    reason: str
    config_version: str
    at: datetime


class JobTrace(BaseModel):
    """Everything core knows about one job. The gateway holds its LLM calls (by job ID)."""

    job: AdminJob
    status: Status
    failure_code: str | None
    retryable: bool
    stalled: bool
    events: list[TraceEvent]
    passages: list[ArticlePassages]
    dropped_quotes: list[DroppedQuote]


class Passage(BaseModel):
    ordinal: int
    section: str
    text: str


class ErrorItem(BaseModel):
    kind: Literal["job_failed", "job_stalled", "quote_dropped", "task_failed"]
    at: datetime
    job_id: UUID | None
    code: str
    detail: str | None
    correlation_id: str | None


class QueueWorker(BaseModel):
    id: int
    last_heartbeat: datetime
    seconds_since: float


class QueueStatus(BaseModel):
    """core-worker's health as the queue sees it: live workers and tasks by status."""

    workers: list[QueueWorker]
    tasks: dict[str, int]


class RetryRequest(BaseModel):
    # web's user ID of the admin who asked, recorded in the job's events.
    actor_user_id: int = Field(ge=1)


def pool_of(request: Request):
    return request.app.state.deps.pool


def _queue(request: Request):
    return request.app.state.deps.queue


def _not_found() -> HTTPException:
    return HTTPException(404, {"code": "not_found", "message": "No such job."})


def _literal(text: str) -> str:
    """The text escaped for LIKE, so % and _ match themselves."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filters(q: str | None, user_ids: list[int]) -> tuple[list[str], list]:
    where, params = [], []
    if user_ids:
        where.append("j.user_id = ANY(%s)")
        params.append(user_ids)
    if q:
        try:
            params.append(UUID(q))
            where.append("j.id = %s")
        except ValueError:
            # An ID's first characters, a correlation ID, or words from the text or the claim.
            where.append(
                "(j.id::text LIKE %s OR j.correlation_id = %s OR j.input_text ILIKE %s"
                " OR j.confirmed_claim->>'supplement' ILIKE %s OR j.confirmed_claim->>'effect' ILIKE %s)"
            )
            pattern = f"%{_literal(q)}%"
            params += [_literal(q.lower()) + "%", q, pattern, pattern, pattern]
    return where, params


async def _all(conn: AsyncConnection, query: str, params=()) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


@router.get("/jobs")
async def list_jobs(
    request: Request,
    status: Status | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    user_id: Annotated[list[int] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> AdminJobList:
    """Every user's jobs, newest first, filtered by status, user and a search over IDs and text."""
    where, params = _filters((q or "").strip() or None, user_id or [])
    page_where = list(where)
    page_params = list(params)
    if status == "in_progress":
        page_where.append("NOT (j.state = ANY(%s))")
        page_params.append(_SETTLED_STATES)
    elif status:
        page_where.append("j.state = ANY(%s)")
        page_params.append(list(SETTLED[status]))
    clause = f"WHERE {' AND '.join(page_where)}" if page_where else ""
    async with pool_of(request).connection() as conn:
        rows = await _all(
            conn,
            f"""
            SELECT j.id, j.user_id, j.state, {_STATUS_SQL} AS status,
                   left(j.input_text, 200) AS input_preview,
                   j.confirmed_claim->>'supplement' AS supplement,
                   j.confirmed_claim->>'effect' AS effect,
                   j.result->>'verdict' AS verdict,
                   round((j.result->>'score')::numeric)::int AS score,
                   {_FAILURE_CODE_SQL} AS failure_code,
                   j.config_version, j.correlation_id, j.created_at, j.updated_at
            FROM core.jobs j {clause}
            ORDER BY j.created_at DESC, j.id
            LIMIT %s OFFSET %s
            """,
            (*page_params, limit + 1, offset),
        )
        count_clause = f"WHERE {' AND '.join(where)}" if where else ""
        counted = await _all(
            conn,
            f"SELECT {_STATUS_SQL} AS status, count(*) AS n FROM core.jobs j {count_clause} GROUP BY 1",
            params,
        )
    counts = dict.fromkeys(STATUSES, 0) | {r["status"]: r["n"] for r in counted}
    return AdminJobList(
        counts=counts,
        jobs=[AdminJobRow(**r) for r in rows[:limit]],
        more=len(rows) > limit,
    )


@router.get("/jobs/{job_id}/trace", responses={404: {"description": "No such job"}})
async def job_trace(job_id: UUID, request: Request) -> JobTrace:
    """The job with its inputs and outputs, each state it went through and for how long, the passages
    stored per article, and the quotes that failed verification."""
    async with pool_of(request).connection() as conn:
        rows = await _all(
            conn,
            f"""
            SELECT j.id, j.user_id, j.state, j.input_type, j.input_text, j.extracted_claims,
                   j.confirmed_claim, j.result, j.error, j.config_version, j.correlation_id,
                   j.created_at, j.updated_at, {_STATUS_SQL} AS status,
                   {_FAILURE_CODE_SQL} AS failure_code, now() AS now
            FROM core.jobs j WHERE j.id = %s
            """,
            (job_id,),
        )
        if not rows:
            raise _not_found()
        events = await _all(
            conn,
            "SELECT state, detail, at FROM core.job_events WHERE job_id = %s ORDER BY id",
            (job_id,),
        )
        passages = await _all(
            conn,
            "SELECT pmcid, count(*) AS passages FROM core.passages WHERE job_id = %s"
            " GROUP BY pmcid ORDER BY min(id)",
            (job_id,),
        )
        dropped = await _all(
            conn,
            "SELECT stage, passage_ref, quote, reason, config_version, at"
            " FROM core.hallucination_events WHERE job_id = %s ORDER BY id",
            (job_id,),
        )
    row = rows[0]
    now, status, failure_code = (
        row.pop("now"),
        row.pop("status"),
        row.pop("failure_code"),
    )
    timed = []
    for i, event in enumerate(events):
        if i + 1 < len(events):
            seconds = (events[i + 1]["at"] - event["at"]).total_seconds()
        else:
            seconds = (
                None if status != "in_progress" else (now - event["at"]).total_seconds()
            )
        timed.append(TraceEvent(**event, seconds=seconds))
    stalled = (
        status == "in_progress"
        and (now - row["updated_at"]).total_seconds() > STALLED_MINUTES * 60
    )
    return JobTrace(
        job=AdminJob(**row),
        status=status,
        failure_code=failure_code,
        retryable=row["state"] == "failed",
        stalled=stalled,
        events=timed,
        passages=[ArticlePassages(**p) for p in passages],
        dropped_quotes=[DroppedQuote(**d) for d in dropped],
    )


@router.get(
    "/jobs/{job_id}/passages",
    responses={404: {"description": "No such job or article"}},
)
async def job_passages(
    job_id: UUID,
    request: Request,
    pmcid: Annotated[str, Query(pattern=r"^PMC\d{1,12}$")],
) -> list[Passage]:
    """Every passage stored for one article of the job, in reading order."""
    async with pool_of(request).connection() as conn:
        rows = await _all(
            conn,
            "SELECT ordinal, section, text FROM core.passages WHERE job_id = %s AND pmcid = %s"
            " ORDER BY ordinal, id",
            (job_id, pmcid),
        )
    if not rows:
        raise HTTPException(
            404,
            {"code": "not_found", "message": "No passages for this job and article."},
        )
    return [Passage(**r) for r in rows]


@router.post(
    "/jobs/{job_id}/retry",
    responses={
        404: {"description": "No such job"},
        409: {"description": "The job is not in failed"},
    },
)
async def retry_job(job_id: UUID, body: RetryRequest, request: Request) -> AdminJob:
    """Sends a failed job back to the start of the phase that failed and queues that task again."""
    async with pool_of(request).connection() as conn, conn.transaction():
        found = await _all(
            conn,
            "SELECT state, confirmed_claim IS NOT NULL AS confirmed, correlation_id"
            " FROM core.jobs WHERE id = %s FOR UPDATE",
            (job_id,),
        )
        if not found:
            raise _not_found()
        if found[0]["state"] != "failed":
            raise HTTPException(
                409,
                {
                    "code": "wrong_state",
                    "message": f"The job is {found[0]['state']}; only failed jobs can be retried.",
                },
            )
        confirmed = found[0]["confirmed"]
        state = "searching" if confirmed else "submitted"
        if confirmed:
            # The search stores each article's passages again; the old ones would be ranked twice.
            await conn.execute("DELETE FROM core.passages WHERE job_id = %s", (job_id,))
        rows = await _all(
            conn,
            "UPDATE core.jobs SET state = %s, error = NULL, result = NULL, updated_at = now()"
            " WHERE id = %s RETURNING id, user_id, state, input_type, input_text, extracted_claims,"
            " confirmed_claim, result, error, config_version, correlation_id, created_at, updated_at",
            (state, job_id),
        )
        await conn.execute(
            "INSERT INTO core.job_events (job_id, state, detail) VALUES (%s, %s, %s)",
            (job_id, state, f"retry by admin user {body.actor_user_id}"),
        )
    # The job keeps its own correlation ID, so its logs stay under one ID across retries.
    job_correlation = found[0]["correlation_id"] or correlation.current()
    task = tasks.RUN if confirmed else tasks.EXTRACT
    await (
        _queue(request)
        .configure_task(task, queue=tasks.QUEUE)
        .defer_async(job_id=str(job_id), correlation_id=job_correlation)
    )
    log.info(
        "job retried by admin",
        extra={
            "job_id": str(job_id),
            "from_state": state,
            "actor_user_id": body.actor_user_id,
            "job_correlation_id": job_correlation,
        },
    )
    return AdminJob(**rows[0])


@router.get("/errors")
async def recent_errors(
    request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[ErrorItem]:
    """Recent failures core records, newest first: failed and stalled jobs, quotes that failed
    verification, and queue tasks that raised. LLM call failures are the gateway's."""
    async with pool_of(request).connection() as conn:
        rows = await _all(
            conn,
            """
            (SELECT 'job_failed' AS kind, e.at, e.job_id, coalesce(e.detail, 'unknown') AS code,
                    NULL AS detail, j.correlation_id
             FROM core.job_events e JOIN core.jobs j ON j.id = e.job_id
             WHERE e.state = 'failed' ORDER BY e.id DESC LIMIT %(limit)s)
            UNION ALL
            (SELECT 'job_stalled', j.updated_at, j.id, j.state, NULL, j.correlation_id
             FROM core.jobs j
             WHERE NOT (j.state = ANY(%(settled)s))
               AND j.updated_at < now() - make_interval(mins => %(stalled)s::int)
             ORDER BY j.updated_at DESC LIMIT %(limit)s)
            UNION ALL
            (SELECT 'quote_dropped', h.at, h.job_id, h.reason, h.passage_ref, j.correlation_id
             FROM core.hallucination_events h JOIN core.jobs j ON j.id = h.job_id
             ORDER BY h.id DESC LIMIT %(limit)s)
            UNION ALL
            (SELECT 'task_failed', coalesce(ev.at, now()), CASE
                      WHEN pj.args->>'job_id' ~ '^[0-9a-f-]{36}$' THEN (pj.args->>'job_id')::uuid
                    END,
                    pj.task_name, 'attempts: ' || pj.attempts, pj.args->>'correlation_id'
             FROM core.procrastinate_jobs pj
             LEFT JOIN LATERAL (SELECT max(at) AS at FROM core.procrastinate_events
                                WHERE job_id = pj.id AND type = 'failed') ev ON true
             WHERE pj.status = 'failed' ORDER BY pj.id DESC LIMIT %(limit)s)
            ORDER BY at DESC LIMIT %(limit)s
            """,
            {"limit": limit, "settled": _SETTLED_STATES, "stalled": STALLED_MINUTES},
        )
    return [ErrorItem(**r) for r in rows]


@router.get("/queue")
async def queue_status(request: Request) -> QueueStatus:
    """Live core-worker processes (Procrastinate heartbeats) and queue tasks by status."""
    async with pool_of(request).connection() as conn:
        workers = await _all(
            conn,
            "SELECT id, last_heartbeat, extract(epoch FROM now() - last_heartbeat)::float AS seconds_since"
            " FROM core.procrastinate_workers ORDER BY id",
        )
        counted = await _all(
            conn,
            "SELECT status::text AS status, count(*) AS n FROM core.procrastinate_jobs GROUP BY 1",
        )
    return QueueStatus(
        workers=[QueueWorker(**w) for w in workers],
        tasks={r["status"]: r["n"] for r in counted},
    )
