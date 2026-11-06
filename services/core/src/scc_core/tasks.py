"""Procrastinate tasks for core-worker (P1.05, P1.08, P3.05, P3.06). Each job carries its correlation ID (P1.01)."""

import logging
import os
from uuid import UUID

from procrastinate import App, Blueprint, PsycopgConnector

from scc_common.correlation import bind
from scc_core import privacy
from scc_core.pipeline import run as pipeline

log = logging.getLogger("scc.tasks")
QUEUE = "pipeline"
EXTRACT = "core:extract_claim"
RUN = "core:run_pipeline"

blueprint = Blueprint()
# Set by the worker at start-up (scc_core.worker) or by tests.
runtime: pipeline.Runtime | None = None


def _rt() -> pipeline.Runtime:
    if runtime is None:
        raise RuntimeError("tasks.runtime is not set")
    return runtime


@blueprint.task(name="extract_claim", queue=QUEUE)
async def extract_claim(job_id: str, correlation_id: str | None = None) -> None:
    with bind(correlation_id):
        await pipeline.extract(_rt(), UUID(job_id))


@blueprint.task(name="run_pipeline", queue=QUEUE)
async def run_pipeline(job_id: str, correlation_id: str | None = None) -> None:
    with bind(correlation_id):
        await pipeline.run(_rt(), UUID(job_id))


@blueprint.periodic(cron="*/5 * * * *")
@blueprint.task(name="sweep_stalled", queue=QUEUE)
async def sweep_stalled(timestamp: int) -> None:
    """Every five minutes: end checks that lost their worker (P3.05, pipeline.run.sweep_stalled)."""
    await pipeline.sweep_stalled(_rt())


@blueprint.periodic(cron="30 3 * * *")
@blueprint.task(name="purge", queue=QUEUE)
async def purge(timestamp: int) -> None:
    """Every night at 03:30 UTC: delete checks and queue entries past their retention (P3.06, D-054)."""
    rt = _rt()
    async with rt.pool.connection() as conn:
        counts = await privacy.purge(conn, rt.config)
    if counts is None:
        log.warning("purge skipped: the active config has no [retention]")
    else:
        log.info("purge finished", extra=counts)


def make_app(conninfo: str) -> App:
    app = App(
        connector=PsycopgConnector(
            conninfo=conninfo,
            kwargs={"application_name": os.environ.get("SCC_SERVICE", "core")},
        )
    )
    app.add_tasks_from(blueprint, namespace="core")
    return app
