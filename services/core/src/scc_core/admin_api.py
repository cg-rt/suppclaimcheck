"""core-api admin endpoints (P3.08, P3.09, P3.03, P4.03, P4.04): jobs by state, per-job traces, recent
errors, the queue, a manual retry for failed jobs, the safety review queue, injection events, and the
outcomes summary (feedback and time to result).

Internal only, for web's admin pages: web may read only its own `auth` schema (D-011), so core data
reaches admin through here. Everything is read-only except the retry and marking a job reviewed.

The retry is deliberately simple: a job in `failed` goes back to the start of the phase that failed
(extraction before confirmation, the search after it) on the config version it was created under,
and the matching Procrastinate task is queued again. The state change is guarded, so two clicks
queue one task. Idempotency keys on internal calls arrive with P3.05.
"""

import logging
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from scc_common import config_store, correlation
from scc_core import config, tasks
from scc_core.models import Feedback

log = logging.getLogger("scc.admin")
router = APIRouter(prefix="/admin", tags=["admin"])

# Jobs by state as admin groups them. Any state not named here is "in progress", so states added
# later still show up somewhere. "refused" is every stop before the search: P3.01's triage refuses
# a request or asks the user to clarify it (needs_clarification).
SETTLED = {
    "done": ("done",),
    "awaiting_confirmation": ("awaiting_confirmation",),
    "failed": ("failed",),
    "refused": ("refused", "needs_clarification"),
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


# What the owner found when reviewing a safety-flagged job (P3.03).
Outcome = Literal["notice_right", "notice_not_needed", "follow_up"]


class Review(BaseModel):
    reviewed_at: datetime
    # web's user ID of the admin who reviewed.
    reviewed_by: int
    outcome: Outcome
    note: str | None


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
    # Safety topics from triage, whether the job waits in the review queue, and its latest review.
    sensitive_topics: list[str] = []
    needs_review: bool = False
    review: Review | None = None
    # P4.04: when the claim was confirmed and when the check finished (migration 0008).
    confirmed_at: datetime | None = None
    finished_at: datetime | None = None


class ReviewRow(AdminJobRow):
    sensitive_topics: list[str]
    needs_review: bool
    review: Review | None


class ReviewList(BaseModel):
    """One page of the safety review queue, and how many jobs are open and reviewed."""

    counts: dict[str, int]
    jobs: list[ReviewRow]
    more: bool


class InjectionEvent(BaseModel):
    """Instructions to the system found in a text (P3.02). The text itself is never stored here."""

    id: int
    at: datetime
    job_id: UUID
    user_id: int
    stage: str
    # `submitted_text` (the user's own text) or the PMCID of the article they were found in.
    source: str
    config_version: str


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
    injection_events: list[InjectionEvent] = []
    feedback: Feedback | None = None


class InjectionAccount(BaseModel):
    """One account's checks whose own text instructed the system: what the throttle counts."""

    user_id: int
    checks_in_window: int
    checks_total: int
    last_at: datetime
    # None when the active config has no throttle (before pipeline-v5).
    throttled: bool | None
    # When the account can start checks again, if it is throttled now.
    throttled_until: datetime | None


class InjectionReport(BaseModel):
    config_version: str
    window_hours: int | None
    max_checks: int | None
    accounts: list[InjectionAccount]
    events: list[InjectionEvent]
    more: bool


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


class ReviewRequest(BaseModel):
    actor_user_id: int = Field(ge=1)
    outcome: Outcome
    # The owner's own words, for the owner; never shown to the user.
    note: str | None = Field(default=None, max_length=500)


# AdminJob's columns, for every query that returns one.
_JOB_COLUMNS = (
    "j.id, j.user_id, j.state, j.input_type, j.input_text, j.extracted_claims, j.confirmed_claim,"
    " j.result, j.error, j.config_version, j.correlation_id, j.created_at, j.updated_at,"
    " j.sensitive_topics, j.needs_review, j.reviewed_at, j.reviewed_by, j.review_outcome,"
    " j.review_note, j.confirmed_at, j.finished_at"
)
_REVIEW_FIELDS = ("reviewed_at", "reviewed_by", "review_outcome", "review_note")


def _with_review(row: dict) -> dict:
    """The row with its four review columns folded into one `review` (None until reviewed)."""
    at, by, outcome, note = (row.pop(k) for k in _REVIEW_FIELDS)
    review = (
        Review(reviewed_at=at, reviewed_by=by, outcome=outcome, note=note)
        if at
        else None
    )
    return {**row, "review": review}


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
            SELECT {_JOB_COLUMNS}, {_STATUS_SQL} AS status,
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
        injections = await _all(
            conn,
            "SELECT id, at, job_id, user_id, stage, source, config_version"
            " FROM core.injection_events WHERE job_id = %s ORDER BY id",
            (job_id,),
        )
        feedback = await _all(
            conn,
            "SELECT verdict_right, understandable, missing, updated_at FROM core.feedback WHERE job_id = %s",
            (job_id,),
        )
    row = _with_review(rows[0])
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
        injection_events=[InjectionEvent(**e) for e in injections],
        feedback=Feedback(**feedback[0]) if feedback else None,
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
            "UPDATE core.jobs j SET state = %s, error = NULL, result = NULL, updated_at = now()"
            f" WHERE j.id = %s RETURNING {_JOB_COLUMNS}",
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
    return AdminJob(**_with_review(rows[0]))


@router.get("/reviews")
async def review_queue(
    request: Request,
    status: Literal["open", "reviewed"] = "open",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> ReviewList:
    """Safety-flagged jobs (P3.03). Open ones oldest first, as a queue; reviewed ones newest first."""
    if status == "open":
        where, order = "j.needs_review", "j.created_at, j.id"
    else:
        where = "j.reviewed_at IS NOT NULL AND NOT j.needs_review"
        order = "j.reviewed_at DESC, j.id"
    async with pool_of(request).connection() as conn:
        rows = await _all(
            conn,
            f"""
            SELECT j.id, j.user_id, j.state, {_STATUS_SQL} AS status,
                   left(j.input_text, 200) AS input_preview,
                   coalesce(j.confirmed_claim->>'supplement',
                            j.extracted_claims->0->>'supplement') AS supplement,
                   coalesce(j.confirmed_claim->>'effect',
                            j.extracted_claims->0->>'effect') AS effect,
                   j.result->>'verdict' AS verdict,
                   round((j.result->>'score')::numeric)::int AS score,
                   {_FAILURE_CODE_SQL} AS failure_code,
                   j.config_version, j.correlation_id, j.created_at, j.updated_at,
                   j.sensitive_topics, j.needs_review, j.reviewed_at, j.reviewed_by,
                   j.review_outcome, j.review_note
            FROM core.jobs j WHERE {where}
            ORDER BY {order}
            LIMIT %s OFFSET %s
            """,
            (limit + 1, offset),
        )
        counted = await _all(
            conn,
            "SELECT count(*) FILTER (WHERE needs_review) AS open,"
            " count(*) FILTER (WHERE reviewed_at IS NOT NULL AND NOT needs_review) AS reviewed"
            " FROM core.jobs",
        )
    return ReviewList(
        counts=counted[0],
        jobs=[ReviewRow(**_with_review(r)) for r in rows[:limit]],
        more=len(rows) > limit,
    )


@router.post(
    "/jobs/{job_id}/review",
    responses={
        404: {"description": "No such job"},
        409: {"description": "The job is not waiting for a review"},
    },
)
async def review_job(job_id: UUID, body: ReviewRequest, request: Request) -> AdminJob:
    """Records the owner's review of a safety-flagged job and takes it off the queue.

    Guarded on `needs_review`, so two clicks record one review. A later flag puts the job back.
    """
    note = (body.note or "").strip() or None
    async with pool_of(request).connection() as conn:
        rows = await _all(
            conn,
            "UPDATE core.jobs j SET needs_review = false, reviewed_at = now(),"
            " reviewed_by = %s, review_outcome = %s, review_note = %s"
            f" WHERE j.id = %s AND j.needs_review RETURNING {_JOB_COLUMNS}",
            (body.actor_user_id, body.outcome, note, job_id),
        )
        if not rows:
            if not await _all(conn, "SELECT 1 FROM core.jobs WHERE id = %s", (job_id,)):
                raise _not_found()
            raise HTTPException(
                409,
                {"code": "not_open", "message": "The job is not waiting for a review."},
            )
    log.info(
        "job reviewed by admin",
        extra={
            "job_id": str(job_id),
            "outcome": body.outcome,
            "actor_user_id": body.actor_user_id,
        },
    )
    return AdminJob(**_with_review(rows[0]))


async def _throttle(request: Request, conn: AsyncConnection) -> tuple[str, dict | None]:
    """The config version new checks start on, and its throttle settings (None before pipeline-v5)."""
    version = request.app.state.deps.config_version or (
        await config_store.active_version_async(conn, config.STORE)
    )
    loaded = await config_store.load_version_async(conn, config.STORE, version)
    return version, loaded.get("guardrails")


def _account(
    user_id: int, times: list[datetime], now: datetime, settings: dict | None
) -> InjectionAccount:
    """One account's standing against the throttle; `times` is each check's latest event, oldest first."""
    if settings is None:
        recent, throttled, until = [], None, None
    else:
        window = timedelta(hours=settings["injection_window_hours"])
        limit = settings["injection_max_checks"]
        # A check leaves the window when its latest event does, as jobs.injection_checks counts.
        recent = [t for t in times if t > now - window]
        throttled = len(recent) >= limit
        # Below the limit again once the oldest (count - limit + 1) checks have left the window.
        until = recent[len(recent) - limit] + window if throttled else None
    return InjectionAccount(
        user_id=user_id,
        checks_in_window=len(recent),
        checks_total=len(times),
        last_at=times[-1],
        throttled=throttled,
        throttled_until=until,
    )


@router.get("/injections")
async def injection_report(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> InjectionReport:
    """Instructions found in texts (P3.02): per account, what the throttle counts and whether it holds
    the account now under the active config, throttled first; then every event, newest first."""
    async with pool_of(request).connection() as conn:
        version, settings = await _throttle(request, conn)
        checks = await _all(
            conn,
            "SELECT user_id, job_id, max(at) AS at FROM core.injection_events"
            " WHERE source = 'submitted_text' GROUP BY user_id, job_id ORDER BY user_id, at",
        )
        events = await _all(
            conn,
            "SELECT id, at, job_id, user_id, stage, source, config_version"
            " FROM core.injection_events ORDER BY at DESC, id DESC LIMIT %s OFFSET %s",
            (limit + 1, offset),
        )
        now = (await _all(conn, "SELECT now() AS now"))[0]["now"]
    by_user: dict[int, list[datetime]] = {}
    for check in checks:
        by_user.setdefault(check["user_id"], []).append(check["at"])
    accounts = [_account(u, times, now, settings) for u, times in by_user.items()]
    accounts.sort(
        key=lambda a: (not a.throttled, -a.checks_in_window, -a.last_at.timestamp())
    )
    return InjectionReport(
        config_version=version,
        window_hours=settings["injection_window_hours"] if settings else None,
        max_checks=settings["injection_max_checks"] if settings else None,
        accounts=accounts,
        events=[InjectionEvent(**e) for e in events[:limit]],
        more=len(events) > limit,
    )


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


class AnswerCount(BaseModel):
    answer: str
    n: int


class FeedbackQuestion(BaseModel):
    question: Literal["verdict_right", "understandable"]
    answers: list[AnswerCount]


class FeedbackComment(BaseModel):
    job_id: UUID
    user_id: int
    verdict: str | None
    verdict_right: str | None
    understandable: str | None
    missing: str
    updated_at: datetime


class Timing(BaseModel):
    measure: Literal["submit_to_result", "confirm_to_result"]
    checks: int
    median_seconds: float | None
    p90_seconds: float | None


class Outcomes(BaseModel):
    """P4.03 and P4.04 over the last `days`: how people rated their results, and how long checks took."""

    days: int
    results: int  # checks that finished with a result in the window
    rated: int  # of those, how many have feedback
    questions: list[FeedbackQuestion]
    comments: list[FeedbackComment]
    timings: list[Timing]
    # Reused results (P3.05) take no time, so they are counted but left out of the timings.
    reused: int


_ANSWERS = {
    "verdict_right": ("yes", "no", "unsure"),
    "understandable": ("yes", "partly", "no"),
}


@router.get("/outcomes")
async def outcomes(
    request: Request, days: Annotated[int, Query(ge=1, le=366)] = 30
) -> Outcomes:
    """Feedback answers and comments, and time to result (median and 90th percentile) for checks
    that finished with a result in the last `days`."""
    window = {"days": days}
    async with pool_of(request).connection() as conn:
        counts = await _all(
            conn,
            """
            SELECT count(*) AS results,
                   count(f.job_id) AS rated,
                   count(*) FILTER (WHERE j.result ? 'reused_from') AS reused
            FROM core.jobs j LEFT JOIN core.feedback f ON f.job_id = j.id
            WHERE j.state = 'done' AND j.finished_at > now() - make_interval(days => %(days)s)
            """,
            window,
        )
        answers = await _all(
            conn,
            """
            SELECT 'verdict_right' AS question, f.verdict_right AS answer, count(*) AS n
            FROM core.feedback f JOIN core.jobs j ON j.id = f.job_id
            WHERE j.finished_at > now() - make_interval(days => %(days)s) AND f.verdict_right IS NOT NULL GROUP BY 2
            UNION ALL
            SELECT 'understandable', f.understandable, count(*)
            FROM core.feedback f JOIN core.jobs j ON j.id = f.job_id
            WHERE j.finished_at > now() - make_interval(days => %(days)s) AND f.understandable IS NOT NULL GROUP BY 2
            """,
            window,
        )
        comments = await _all(
            conn,
            """
            SELECT f.job_id, f.user_id, j.result->>'verdict' AS verdict, f.verdict_right,
                   f.understandable, f.missing, f.updated_at
            FROM core.feedback f JOIN core.jobs j ON j.id = f.job_id
            WHERE j.finished_at > now() - make_interval(days => %(days)s) AND f.missing IS NOT NULL
            ORDER BY f.updated_at DESC LIMIT 50
            """,
            window,
        )
        timings = await _all(
            conn,
            """
            WITH t AS (
                SELECT extract(epoch FROM j.finished_at - j.created_at) AS submit_to_result,
                       extract(epoch FROM j.finished_at - j.confirmed_at) AS confirm_to_result
                FROM core.jobs j
                WHERE j.state = 'done' AND j.finished_at > now() - make_interval(days => %(days)s) AND NOT j.result ? 'reused_from'
            )
            SELECT 'submit_to_result' AS measure, count(submit_to_result) AS checks,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY submit_to_result) AS median_seconds,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY submit_to_result) AS p90_seconds
            FROM t
            UNION ALL
            SELECT 'confirm_to_result', count(confirm_to_result),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY confirm_to_result),
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY confirm_to_result)
            FROM t
            """,
            window,
        )
    found = {(a["question"], a["answer"]): a["n"] for a in answers}
    return Outcomes(
        days=days,
        **counts[0],
        questions=[
            FeedbackQuestion(
                question=question,
                answers=[
                    AnswerCount(answer=a, n=found.get((question, a), 0))
                    for a in choices
                ],
            )
            for question, choices in _ANSWERS.items()
        ],
        comments=[FeedbackComment(**c) for c in comments],
        timings=[Timing(**t) for t in timings],
    )
