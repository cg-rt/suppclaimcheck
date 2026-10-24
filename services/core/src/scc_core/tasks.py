"""Procrastinate tasks for core-worker (P1.05, P1.08). Each job carries its correlation ID (P1.01)."""

import os
from uuid import UUID

from procrastinate import App, Blueprint, PsycopgConnector

from scc_common.correlation import bind
from scc_core.pipeline import run as pipeline

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


def make_app(conninfo: str) -> App:
    app = App(
        connector=PsycopgConnector(
            conninfo=conninfo,
            kwargs={"application_name": os.environ.get("SCC_SERVICE", "core")},
        )
    )
    app.add_tasks_from(blueprint, namespace="core")
    return app
