"""Jobs and LLM calls written straight into core's and the gateway's tables, in the shapes the
pipeline and the gateway leave them, for the admin tests (P3.08-P3.10)."""

import uuid
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from tests.web_samples import CLAIM, RESULT

NOW = datetime.now(UTC)


async def _job(
    conn, state, *, user_id, claim=None, result=None, minutes_ago=1, text=None
):
    job_id = uuid.uuid4()
    at = NOW - timedelta(minutes=minutes_ago)
    await conn.execute(
        "INSERT INTO core.jobs (id, user_id, idempotency_key, request_hash, state, input_type,"
        " input_text, confirmed_claim, result, error, config_version, correlation_id,"
        " created_at, updated_at) VALUES (%s, %s, %s, 'h', %s, 'text', %s, %s, %s, %s,"
        " 'pipeline-v4', %s, %s, %s)",
        (
            job_id,
            user_id,
            f"key-{job_id}",
            state,
            text or "Creatine makes older adults stronger.",
            Jsonb(claim) if claim else None,
            Jsonb(result) if result else None,
            "The language model service is not answering."
            if state == "failed"
            else None,
            f"corr{job_id.hex[:12]}",
            at,
            at,
        ),
    )
    return job_id, at


async def _event(conn, job_id, state, at, detail=None):
    await conn.execute(
        "INSERT INTO core.job_events (job_id, state, detail, at) VALUES (%s, %s, %s, %s)",
        (job_id, state, detail, at),
    )


async def seed(conn, user: int = 7, other: int = 8, clear: bool = True) -> dict:
    """Five jobs, one per admin status, plus a failure before confirmation. `other` owns one job."""
    if clear:
        for table in ("hallucination_events", "passages", "job_events", "jobs"):
            await conn.execute(f"DELETE FROM core.{table}")
        await conn.execute("DELETE FROM core.procrastinate_workers")
    ids = {}
    # The pipeline records the passages each assess call read (`retrieved`, P3.01 onwards).
    result = {**RESULT, "retrieved": {"PMC6543210": ["PMC6543210-0", "PMC6543210-2"]}}
    ids["done"], at = await _job(
        conn, "done", user_id=user, claim=CLAIM, result=result, minutes_ago=50
    )
    for ordinal in range(3):
        await conn.execute(
            "INSERT INTO core.passages (job_id, pmcid, ordinal, section, text) VALUES (%s, %s, %s, %s, %s)",
            (ids["done"], "PMC6543210", ordinal, "Results", f"Done passage {ordinal}."),
        )
    for i, state in enumerate(
        ("submitted", "extracting", "awaiting_confirmation", "searching")
    ):
        await _event(conn, ids["done"], state, at + timedelta(seconds=10 * i))
    await _event(conn, ids["done"], "done", at + timedelta(seconds=60), "Supported")
    await conn.execute(
        "INSERT INTO core.hallucination_events (job_id, stage, passage_ref, quote, reason, config_version)"
        " VALUES (%s, 'assess', 'PMC6543210-7', 'Made-up words.', 'not_in_passage', 'pipeline-v4')",
        (ids["done"],),
    )

    ids["failed_late"], at = await _job(
        conn, "failed", user_id=other, claim=CLAIM, minutes_ago=40
    )
    await _event(conn, ids["failed_late"], "submitted", at)
    await _event(
        conn, ids["failed_late"], "searching", at + timedelta(seconds=5), "confirmed"
    )
    await _event(
        conn,
        ids["failed_late"],
        "failed",
        at + timedelta(seconds=9),
        "provider_unavailable",
    )
    for ordinal in range(3):
        await conn.execute(
            "INSERT INTO core.passages (job_id, pmcid, ordinal, section, text) VALUES (%s, %s, %s, %s, %s)",
            (
                ids["failed_late"],
                "PMC6543210",
                ordinal,
                "Results",
                f"Passage {ordinal}.",
            ),
        )

    ids["failed_early"], at = await _job(
        conn, "failed", user_id=user, minutes_ago=30, text="Zinc and colds."
    )
    await _event(conn, ids["failed_early"], "submitted", at)
    await _event(
        conn, ids["failed_early"], "failed", at + timedelta(seconds=3), "invalid_output"
    )

    ids["stalled"], at = await _job(
        conn, "assessing", user_id=user, claim=CLAIM, minutes_ago=20
    )
    await _event(conn, ids["stalled"], "assessing", at)

    ids["waiting"], at = await _job(
        conn,
        "awaiting_confirmation",
        user_id=user,
        minutes_ago=1,
        text="Magnesium helps sleep.",
    )
    await _event(conn, ids["waiting"], "awaiting_confirmation", at, "1 claim(s)")
    return ids


async def seed_calls(conn, job_id: str, clear: bool = True) -> None:
    """Gateway call records: two for `job_id` (one failed), one eval call, one from long ago."""
    if clear:
        await conn.execute("DELETE FROM gateway.llm_calls")
    rows = [
        ("live", "ok", "extract", job_id, 100, 20, 0.0002, 900, None, 0),
        ("live", "failed", "assess", job_id, None, None, 0.0, 120, "provider_unavailable: ReadTimeout", 0),
        ("eval", "ok", "assess", "eval-case-03", 3000, 200, 0.003, 2500, None, 0),
        ("live", "ok", "extract", "job-old", 100, 20, 0.5, 800, None, 70),
    ]  # fmt: skip
    for lane, status, stage, job, tin, tout, cost, ms, error, days_ago in rows:
        await conn.execute(
            "INSERT INTO gateway.llm_calls (lane, status, stage, job_id, user_id, model,"
            " input_tokens, output_tokens, cost_eur, latency_ms, error, correlation_id,"
            " created_at) VALUES (%s, %s, %s, %s, 7, 'claude-haiku-5-5', %s, %s, %s, %s,"
            " %s, 'corr-1234', now() - make_interval(days => %s))",
            (lane, status, stage, job, tin, tout, cost, ms, error, days_ago),
        )
