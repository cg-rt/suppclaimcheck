"""Gateway admin endpoints (P3.08, P3.09): the LLM call records, read-only.

Internal only, for web's admin pages. Every LLM call has a row in `gateway.llm_calls` (AGENTS.md
section 8); these endpoints list them and add them up. Budget roll-ups and the 50% and 80% warnings
belong to the budget guard (P3.04), not here.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import BaseModel

from scc_gateway import budget

router = APIRouter(prefix="/admin", tags=["admin"])
LANES = ("live", "eval")
# The month starts in UTC, as the budget cap counts it (scc_gateway.budget.reserve).
_MONTH_START = "date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"


class LlmCall(BaseModel):
    id: int
    lane: str
    status: str
    stage: str
    job_id: str | None
    user_id: int | None
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_eur: float
    latency_ms: int | None
    error: str | None
    correlation_id: str | None
    created_at: datetime


class LaneMonth(BaseModel):
    lane: str
    cap_eur: float
    # Counted as the cap counts it: a call still running holds its reserved worst case.
    spent_eur: float
    calls: int
    failed: int
    running: int
    # Distinct jobs that made a call: the denominator for cost per check.
    jobs: int
    input_tokens: int
    output_tokens: int


class DayUsage(BaseModel):
    day: date
    lane: str
    calls: int
    cost_eur: float
    input_tokens: int
    output_tokens: int


class StageUsage(BaseModel):
    lane: str
    stage: str
    model: str
    calls: int
    failed: int
    cost_eur: float
    input_tokens: int
    output_tokens: int
    median_latency_ms: float | None
    p95_latency_ms: float | None


class Usage(BaseModel):
    """This month's LLM spend per lane, the last `days` days, and this month by stage and model."""

    month_start: datetime
    lanes: list[LaneMonth]
    days: list[DayUsage]
    stages: list[StageUsage]


def pool_of(request: Request):
    return request.app.state.deps.pool


async def _all(conn: AsyncConnection, query: str, params=()) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


@router.get("/llm-calls")
async def llm_calls(
    request: Request,
    job_id: Annotated[str | None, Query(max_length=64)] = None,
    status: Literal["reserved", "ok", "failed"] | None = None,
    lane: Literal["live", "eval"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[LlmCall]:
    """LLM calls, newest first. With `job_id`, every call that job made, oldest first."""
    where, params = [], []
    for column, value in (("job_id", job_id), ("status", status), ("lane", lane)):
        if value is not None:
            where.append(f"{column} = %s")
            params.append(value)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = "id" if job_id else "id DESC"
    async with pool_of(request).connection() as conn:
        rows = await _all(
            conn,
            "SELECT id, lane, status, stage, job_id, user_id, model, input_tokens, output_tokens,"
            f" cost_eur::float AS cost_eur, latency_ms, error, correlation_id, created_at"
            f" FROM gateway.llm_calls {clause} ORDER BY {order} LIMIT %s",
            (*params, limit),
        )
    return [LlmCall(**r) for r in rows]


@router.get("/usage")
async def usage(
    request: Request, days: Annotated[int, Query(ge=1, le=92)] = 14
) -> Usage:
    """Spend, calls and tokens from the LLM call records; caps from the active gateway config."""
    config = request.app.state.deps.config
    async with pool_of(request).connection() as conn:
        month = await _all(conn, f"SELECT {_MONTH_START} AS start")
        lanes = await _all(
            conn,
            f"""
            SELECT lane, coalesce(sum(cost_eur), 0)::float AS spent_eur, count(*) AS calls,
                   count(*) FILTER (WHERE status = 'failed') AS failed,
                   count(*) FILTER (WHERE status = 'reserved') AS running,
                   count(DISTINCT job_id) AS jobs,
                   coalesce(sum(input_tokens), 0) AS input_tokens,
                   coalesce(sum(output_tokens), 0) AS output_tokens
            FROM gateway.llm_calls WHERE created_at >= {_MONTH_START} GROUP BY lane
            """,
        )
        by_day = await _all(
            conn,
            """
            SELECT (created_at AT TIME ZONE 'UTC')::date AS day, lane, count(*) AS calls,
                   coalesce(sum(cost_eur), 0)::float AS cost_eur,
                   coalesce(sum(input_tokens), 0) AS input_tokens,
                   coalesce(sum(output_tokens), 0) AS output_tokens
            FROM gateway.llm_calls
            WHERE created_at >= (((now() AT TIME ZONE 'UTC')::date - %s::int)::timestamp AT TIME ZONE 'UTC')
            GROUP BY 1, 2 ORDER BY 1 DESC, 2
            """,
            (days - 1,),
        )
        stages = await _all(
            conn,
            f"""
            SELECT lane, stage, model, count(*) AS calls,
                   count(*) FILTER (WHERE status = 'failed') AS failed,
                   coalesce(sum(cost_eur), 0)::float AS cost_eur,
                   coalesce(sum(input_tokens), 0) AS input_tokens,
                   coalesce(sum(output_tokens), 0) AS output_tokens,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS median_latency_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms
            FROM gateway.llm_calls WHERE created_at >= {_MONTH_START}
            GROUP BY 1, 2, 3 ORDER BY 1, 6 DESC
            """,
        )
    found = {r.pop("lane"): r for r in lanes}
    empty = {
        "spent_eur": 0.0,
        "calls": 0,
        "failed": 0,
        "running": 0,
        "jobs": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    return Usage(
        month_start=month[0]["start"],
        lanes=[
            LaneMonth(
                **(empty | found.get(lane, {})),
                lane=lane,
                cap_eur=budget.cap_for(lane, config),
            )
            for lane in LANES
        ],
        days=[DayUsage(**r) for r in by_day],
        stages=[StageUsage(**r) for r in stages],
    )
