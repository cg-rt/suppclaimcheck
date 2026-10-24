"""core-api: jobs and confirmation (P1.05). Internal only: web calls it on the internal network."""

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response, status
from procrastinate import App
from psycopg_pool import AsyncConnectionPool

from scc_common import correlation
from scc_common.app import Check, create_app, http_check
from scc_common.db import conninfo_from_env, db_check
from scc_core import config, jobs, tasks
from scc_core.models import Claim, Job, JobCreate

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
    config_version: str


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
        try:
            job, created = await jobs.create(
                conn, body, idempotency_key, deps.config_version, correlation.current()
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


@router.get("/{job_id}", responses={404: {"description": "No such job"}})
async def get_job(job_id: UUID, request: Request) -> Job:
    async with _deps(request).pool.connection() as conn:
        job = await jobs.get(conn, job_id)
    if job is None:
        raise HTTPException(404, {"code": "not_found", "message": "No such job."})
    return job


@router.post(
    "/{job_id}/confirm",
    responses={
        404: {"description": "No such job"},
        409: {"description": "The job is not waiting for confirmation"},
    },
)
async def confirm_job(job_id: UUID, claim: Claim, request: Request) -> Job:
    """Stores the claim as the user confirmed or edited it, then queues the search. Nothing is searched before this."""
    deps = _deps(request)
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
            config_version=config.load()["version"],
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
    return app
