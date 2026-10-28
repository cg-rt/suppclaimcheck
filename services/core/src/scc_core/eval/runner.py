"""Runs eval cases through the real pipeline (P2.02).

Each case goes through the same functions core-worker runs, in order: extract, auto-confirm the first
extracted claim (as a user would), search, retrieve, assess, verify, score. Only two things differ from
production: the jobs live in a throwaway database that is dropped afterwards, and every gateway call
goes through `EvalTransport`, which charges the eval budget lane and records or replays the replies.
"""

import dataclasses
import json
import logging
import secrets
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from scc_common.correlation import bind
from scc_common.migrations import migrate
from scc_core import jobs
from scc_core import migrate as core_migrate
from scc_core.eval.cases import EvalCase
from scc_core.eval.metrics import CaseResult, Observation, judge, stale_result
from scc_core.eval.replay import EvalTransport, StaleRecording
from scc_core.gateway_client import GatewayClient
from scc_core.models import Claim, Job, JobCreate, JobState
from scc_core.pipeline import run as pipeline

log = logging.getLogger("scc.eval")

# Jobs made by the eval runner belong to user 0, which no real account can have.
EVAL_USER_ID = 0


class RunAborted(Exception):
    """The gateway could not be reached: no point running the remaining cases."""


@contextmanager
def scratch_database(admin_dsn: str) -> Iterator[str]:
    """A new database with core's schema and roles, dropped afterwards. Yields core's app-role conninfo."""
    suffix = uuid.uuid4().hex[:8]
    dbname = f"scc_eval_{suffix}"
    spec = dataclasses.replace(core_migrate.SPEC, prefix=f"e{suffix}_")
    password = secrets.token_urlsafe(18)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    admin_conninfo = psycopg.conninfo.make_conninfo(admin_dsn, dbname=dbname)
    try:
        migrate(spec, core_migrate.MIGRATIONS, admin_conninfo, password)
        yield psycopg.conninfo.make_conninfo(
            admin_conninfo, user=spec.app_role, password=password
        )
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(dbname)
                )
            )
            for role in (spec.app_role, spec.owner_role):
                admin.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )


def observe(
    job: Job, last_detail: str | None, transport: EvalTransport, latency_s: float | None
) -> Observation:
    claims = job.extracted_claims or []
    result = job.result or {}
    if job.state == JobState.refused:
        behaviour = "refuse"
    elif job.state == JobState.failed:
        behaviour = "no_claim" if last_detail == "no_claim" else "error"
    elif job.state == JobState.awaiting_confirmation:
        behaviour = "pick_claim" if len(claims) >= 2 else "single_claim"
    elif job.state == JobState.done:
        behaviour = (
            "answer_with_safety_notice" if result.get("safety_notice") else "answer"
        )
    else:
        behaviour = "error"
    return Observation(
        state=job.state.value,
        behaviour=behaviour,
        claims=claims,
        error_code=last_detail if job.state == JobState.failed else None,
        verdict=result.get("verdict"),
        score=result.get("score"),
        quotes=result.get("quotes", []),
        quotes_dropped=result.get("quotes_dropped", 0),
        article_ids={a["pmcid"]: a.get("pmid") for a in result.get("articles", [])},
        # Query v1 (P2.12) adds the spelling suggestion; until then no case can observe one.
        spelling_suggestion=None,
        latency_s=round(latency_s, 2) if latency_s is not None else None,
        cost_eur=round(transport.cost_eur.get(str(job.id), 0.0), 6),
        llm_calls=transport.llm_calls.get(str(job.id), 0),
        output_text=json.dumps(
            {"claims": claims, "result": result, "error": job.error}, ensure_ascii=False
        ),
    )


async def run_case(
    rt: pipeline.Runtime, transport: EvalTransport, case: EvalCase, run_id: str
) -> CaseResult:
    correlation_id = f"eval-{run_id}-{case.id}"[:64]
    latency_s = None
    try:
        with bind(correlation_id):
            async with rt.pool.connection() as conn:
                job, _ = await jobs.create(
                    conn,
                    JobCreate(user_id=EVAL_USER_ID, input_text=case.input),
                    f"{run_id}-{case.id}",
                    rt.config["version"],
                    correlation_id,
                )
            await pipeline.extract(rt, job.id)
            async with rt.pool.connection() as conn:
                job = await jobs.get(conn, job.id)
            # A multi-claim case stops at the confirm page, where the user would pick one.
            if (
                job.state == JobState.awaiting_confirmation
                and case.expected.behaviour != "pick_claim"
            ):
                first = job.extracted_claims[0]
                claim = Claim(**{k: first.get(k) for k in Claim.model_fields})
                async with rt.pool.connection() as conn:
                    await jobs.confirm(conn, job.id, claim)
                started = time.monotonic()
                await pipeline.run(rt, job.id)
                if transport.mode == "live":
                    latency_s = time.monotonic() - started
    except StaleRecording as exc:
        return stale_result(case, str(exc))
    except httpx.TransportError as exc:
        raise RunAborted(f"gateway unreachable: {type(exc).__name__}") from exc
    async with rt.pool.connection() as conn:
        job = await jobs.get(conn, job.id)
        events = await jobs.events(conn, job.id)
    return judge(
        case, observe(job, events[-1].detail if events else None, transport, latency_s)
    )


async def run_cases(
    cases: list[EvalCase],
    config: dict,
    transport: EvalTransport,
    conninfo: str,
    run_id: str,
    gateway_url: str = "http://gateway",
    progress=print,
) -> list[CaseResult]:
    """Runs the cases one at a time, so identical requests replay in the order they were recorded."""
    pool = AsyncConnectionPool(conninfo, open=False, min_size=1, max_size=4)
    await pool.open()
    gateway = GatewayClient(gateway_url, transport=transport, lane="eval")
    rt = pipeline.Runtime(pool=pool, gateway=gateway, config=config)
    results = []
    try:
        for case in cases:
            result = await run_case(rt, transport, case, run_id)
            mark = "stale" if result.stale else "pass" if result.passed else "FAIL"
            progress(f"  {mark:5} {case.id:18} {'; '.join(result.reasons)}")
            results.append(result)
    finally:
        await pool.close()
        await transport.shutdown()
    return results
