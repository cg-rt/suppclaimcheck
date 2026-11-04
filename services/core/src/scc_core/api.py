"""core-api: jobs and confirmation (P1.05). Internal only: web calls it on the internal network."""

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from procrastinate import App
from psycopg_pool import AsyncConnectionPool

from scc_common import config_admin, config_store, correlation
from scc_common.app import Check, create_app, http_check
from scc_common.db import conninfo_from_env, db_check
from scc_core import admin_api, config, jobs, tasks
from scc_core.gateway_client import GatewayClient, GatewayError
from scc_core.models import Claim, Job, JobCreate, JobEvent, JobSummary
from scc_core.pipeline.triage import THROTTLED

log = logging.getLogger("scc.jobs")
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


@dataclass
class Deps:
    pool: AsyncConnectionPool
    queue: App
    # Tests pin a version; otherwise each new job gets the version active when it is created (D-048).
    config_version: str | None = None
    # Asked before a new job or a confirmation whether the budget allows it (P3.04, D-053). Tests that
    # don't test the budget leave it out.
    gateway: GatewayClient | None = None


router = APIRouter(prefix="/jobs", tags=["jobs"])


def _deps(request: Request) -> Deps:
    return request.app.state.deps


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "description": "Same key and body as an earlier request: the existing job"
        },
        409: {"description": "Key reused with a different body"},
        402: {"description": "This month's budget for checks is used up"},
        429: {
            "description": "The account's checks for today are used up, or too many recent checks "
            "contained instructions to the system"
        },
        503: {"description": "The budget could not be checked"},
    },
)
async def create_job(
    body: JobCreate,
    idempotency_key: IdempotencyKey,
    request: Request,
    response: Response,
) -> Job:
    """Creates a job and queues claim extraction. Retrying with the same Idempotency-Key returns the same job."""
    deps = _deps(request)
    async with deps.pool.connection() as conn:
        version = deps.config_version or await config_store.active_version_async(
            conn, config.STORE
        )
        await _refuse_if_throttled(conn, body.user_id, idempotency_key, version)
        if not await jobs.exists(conn, body.user_id, idempotency_key):
            await _refuse_if_over_budget(deps, body.user_id)
        try:
            job, created = await jobs.create(
                conn, body, idempotency_key, version, correlation.current()
            )
        except jobs.IdempotencyConflict:
            raise HTTPException(
                409,
                {
                    "code": "idempotency_conflict",
                    "message": "This Idempotency-Key was used for a different request.",
                },
            ) from None
    if not created:
        response.status_code = status.HTTP_200_OK
        return job
    await deps.queue.configure_task(tasks.EXTRACT, queue=tasks.QUEUE).defer_async(
        job_id=str(job.id), correlation_id=correlation.current()
    )
    log.info("job created", extra={"job_id": str(job.id)})
    return job


async def _refuse_if_throttled(conn, user_id: int, key: str, version: str) -> None:
    """P3.02: an account whose recent texts kept instructing the system can't start new checks for a while.
    A retry of a check that already exists is not a new check."""
    settings = (await config_store.load_version_async(conn, config.STORE, version)).get(
        "guardrails"
    )
    if settings is None or await jobs.exists(conn, user_id, key):
        return
    checks = await jobs.injection_checks(
        conn, user_id, settings["injection_window_hours"]
    )
    if checks >= settings["injection_max_checks"]:
        log.warning("job refused: injection throttle", extra={"user_id": user_id})
        raise HTTPException(429, {"code": "injection_throttled", "message": THROTTLED})


# HTTP status for each reason the budget refuses a new check (P3.04).
_BUDGET_STATUS = {"budget_cap_reached": 402, "daily_quota_reached": 429}
BUDGET_UNAVAILABLE = "Checks can't start right now because the budget could not be checked. Please try again in a few minutes."


async def _refuse_if_over_budget(deps: Deps, user_id: int | None) -> None:
    """P3.04: no new check (or search, without a user) once the live budget is nearly used, and no new
    check once the account's daily quota is used. The gateway keeps both; if it can't answer, nothing
    starts that would fail at its first LLM call anyway (fails closed)."""
    if deps.gateway is None:
        return
    try:
        status_ = await deps.gateway.budget_status(user_id)
    except (GatewayError, httpx.HTTPError) as exc:
        log.warning("budget check failed", extra={"error": type(exc).__name__})
        raise HTTPException(
            503, {"code": "budget_unavailable", "message": BUDGET_UNAVAILABLE}
        ) from None
    code = status_["code"]
    if user_id is None and code == "daily_quota_reached":
        return
    if not status_["new_checks_allowed"] and code in _BUDGET_STATUS:
        log.info("job refused by the budget", extra={"code": code, "user_id": user_id})
        raise HTTPException(
            _BUDGET_STATUS[code], {"code": code, "message": status_["message"]}
        )


@router.get("")
async def list_jobs(
    user_id: int, request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[JobSummary]:
    """A user's jobs, newest first."""
    async with _deps(request).pool.connection() as conn:
        return await jobs.list_for_user(conn, user_id, limit)


@router.get("/{job_id}", responses={404: {"description": "No such job"}})
async def get_job(job_id: UUID, request: Request) -> Job:
    async with _deps(request).pool.connection() as conn:
        job = await jobs.get(conn, job_id)
    if job is None:
        raise HTTPException(404, {"code": "not_found", "message": "No such job."})
    return job


@router.get("/{job_id}/events", responses={404: {"description": "No such job"}})
async def get_job_events(job_id: UUID, request: Request) -> list[JobEvent]:
    """Every state the job has been in, oldest first: the progress page reads these."""
    async with _deps(request).pool.connection() as conn:
        events = await jobs.events(conn, job_id)
    if not events:
        raise HTTPException(404, {"code": "not_found", "message": "No such job."})
    return events


@router.post(
    "/{job_id}/confirm",
    responses={
        402: {"description": "This month's budget for checks is used up"},
        404: {"description": "No such job"},
        409: {"description": "The job is not waiting for confirmation"},
        503: {"description": "The budget could not be checked"},
    },
)
async def confirm_job(job_id: UUID, claim: Claim, request: Request) -> Job:
    """Stores the claim as the user confirmed or edited it, then queues the search. Nothing is searched before this."""
    deps = _deps(request)
    # The search spends most of a check: it doesn't start when the month's budget is nearly used.
    await _refuse_if_over_budget(deps, None)
    async with deps.pool.connection() as conn:
        try:
            job = await jobs.confirm(conn, job_id, claim)
        except LookupError:
            raise HTTPException(
                404, {"code": "not_found", "message": "No such job."}
            ) from None
        except jobs.WrongState as exc:
            raise HTTPException(
                409,
                {
                    "code": "wrong_state",
                    "message": f"The job is {exc.state}, not waiting for confirmation.",
                },
            ) from None
    await deps.queue.configure_task(tasks.RUN, queue=tasks.QUEUE).defer_async(
        job_id=str(job_id), correlation_id=correlation.current()
    )
    return job


def build_app(
    checks: dict[str, Check] | None = None, deps: Deps | None = None
) -> FastAPI:
    if checks is None:
        conninfo = conninfo_from_env()
        checks = {
            "database": db_check(conninfo),
            "gateway": http_check(os.environ["SCC_GATEWAY_URL"]),
        }
    if deps is None and "SCC_DB_USER" in os.environ:
        conninfo = conninfo_from_env()
        deps = Deps(
            pool=AsyncConnectionPool(conninfo, open=False, min_size=1, max_size=5),
            queue=tasks.make_app(conninfo),
            gateway=GatewayClient(os.environ["SCC_GATEWAY_URL"]),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if deps is not None:
            await deps.pool.open()
            await deps.queue.open_async()
        try:
            yield
        finally:
            if deps is not None:
                await deps.queue.close_async()
                await deps.pool.close()

    app = create_app("core-api", "SuppClaimCheck core API", checks, lifespan=lifespan)
    app.state.deps = deps
    app.include_router(router)
    # Read-only admin views for web's admin pages (P3.08-P3.10), and the one retry action.
    app.include_router(admin_api.router)
    app.include_router(config_admin.router(config.STORE, admin_api.pool_of))
    return app
