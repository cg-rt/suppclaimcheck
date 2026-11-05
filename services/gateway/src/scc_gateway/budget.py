"""Cost of LLM calls, the monthly hard cap (P1.06, D-021), and the full budget guard (P3.04, D-053).

Before each call, a row is reserved at the call's worst-case cost (estimated input, full max_tokens
output) under an advisory lock, so two calls at once can't both slip under the cap. After the call
the row gets the real tokens and cost. If the database can't be reached, no call is made. Fails closed.

Around it: a level per lane (ok, warn_50, warn_80, cap) for core's check before a new job and for
admin's banner; one warning per level and month; and a per-account daily quota of checks, counted
from the live lane's extraction calls (one per check).
"""

from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


class UnknownModel(Exception):
    pass


# The start of this month and of today, in UTC, as SQL.
MONTH_START = "date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
DAY_START = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
# The stage that starts a check: its calls count against the daily quota.
CHECK_STAGE = "extract"


class QuotaReached(Exception):
    def __init__(self, user_id: int, used: int, quota: int):
        super().__init__(f"user {user_id}: {used} of {quota} checks today")
        self.user_id, self.used, self.quota = user_id, used, quota


class CapReached(Exception):
    def __init__(self, lane: str, spent: float, cap: float):
        super().__init__(
            f"{lane} budget: spent {spent:.4f} of {cap:.2f} EUR this month"
        )
        self.lane, self.spent, self.cap = lane, spent, cap


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


def cost_eur(model: str, usage: Usage, config: dict) -> float:
    """Cost of one call from the config price table. Long prompts switch the whole call to the long-prompt rates."""
    prices = config["prices"].get(model)
    if prices is None:
        raise UnknownModel(model)
    long = usage.input_tokens > prices.get("long_prompt_threshold_tokens", float("inf"))
    input_rate = (
        prices["long_input_usd_per_mtok"] if long else prices["input_usd_per_mtok"]
    )
    output_rate = (
        prices["long_output_usd_per_mtok"] if long else prices["output_usd_per_mtok"]
    )
    usd = (
        usage.input_tokens * input_rate + usage.output_tokens * output_rate
    ) / 1_000_000
    return usd * config["budget"]["eur_per_usd"]


def worst_case_eur(
    model: str, input_chars: int, max_tokens: int, config: dict
) -> float:
    estimated_input = input_chars // config["budget"]["chars_per_token_estimate"] + 1
    return cost_eur(model, Usage(estimated_input, max_tokens), config)


def cap_for(lane: str, config: dict) -> float:
    return config["budget"][f"{lane}_monthly_cap_eur"]


async def reserve(
    conn: AsyncConnection, *, lane: str, reserve_eur: float, config: dict, record: dict
) -> int:
    """Checks the cap and reserves a row for the call; returns its id. Raises CapReached."""
    cap = cap_for(lane, config)
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"scc-budget-{lane}",)
        )
        spent = await month_spend(conn, lane)
        if spent + reserve_eur > cap:
            raise CapReached(lane, spent, cap)
        cur = await conn.execute(
            """
            INSERT INTO gateway.llm_calls (lane, status, stage, job_id, user_id, model, cost_eur,
                                           correlation_id, idempotency_key)
            VALUES (%s, 'reserved', %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                lane,
                record["stage"],
                record["job_id"],
                record["user_id"],
                record["model"],
                reserve_eur,
                record["correlation_id"],
                record.get("idempotency_key"),
            ),
        )
        return (await cur.fetchone())[0]


async def settle(
    conn: AsyncConnection,
    call_id: int,
    *,
    status: str,
    usage: Usage | None,
    cost: float,
    latency_ms: int,
    error: str | None,
    output: dict | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE gateway.llm_calls
        SET status = %s, input_tokens = %s, output_tokens = %s, cost_eur = %s, latency_ms = %s,
            error = %s, output = %s
        WHERE id = %s
        """,
        (
            status,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            cost,
            latency_ms,
            error,
            Jsonb(output) if output is not None else None,
            call_id,
        ),
    )


async def month_spend(conn: AsyncConnection, lane: str) -> float:
    """This month's spend in the lane, counting a running call at its reserved worst case."""
    cur = await conn.execute(
        "SELECT coalesce(sum(cost_eur), 0) FROM gateway.llm_calls"
        f" WHERE lane = %s AND created_at >= {MONTH_START}",
        (lane,),
    )
    return float((await cur.fetchone())[0])


def warning_name(share: float) -> str:
    return f"warn_{round(share * 100)}"


def level(lane: str, spent: float, config: dict) -> str:
    """ok, a warning (warn_50, warn_80 from the config's shares), or cap: too little left for a check."""
    cap = cap_for(lane, config)
    if spent >= cap or cap - spent < config["budget"].get("new_check_reserve_eur", 0.0):
        return "cap"
    for share in sorted(config["budget"].get("warn_at", []), reverse=True):
        if spent >= share * cap:
            return warning_name(share)
    return "ok"


async def warnings_due(conn: AsyncConnection, lane: str, config: dict) -> list[str]:
    """The warning levels the lane has passed this month without a warning yet; recorded as sent."""
    spent = await month_spend(conn, lane)
    cap = cap_for(lane, config)
    due = []
    for share in sorted(config["budget"].get("warn_at", [])):
        if spent < share * cap:
            break
        cur = await conn.execute(
            "INSERT INTO gateway.budget_alerts (lane, month, level, spent_eur)"
            f" VALUES (%s, ({MONTH_START})::date, %s, %s)"
            " ON CONFLICT DO NOTHING RETURNING level",
            (lane, warning_name(share), spent),
        )
        if await cur.fetchone():
            due.append(warning_name(share))
    return due


def quota_for(user_id: int | None, config: dict) -> int | None:
    """The account's daily quota of checks, or None when it has none (off, exempt, or no account)."""
    quotas = config.get("quotas", {})
    limit = quotas.get("checks_per_user_per_day", 0)
    if user_id is None or not limit or user_id in quotas.get("exempt_user_ids", []):
        return None
    return limit


async def checks_today(
    conn: AsyncConnection, user_id: int, other_than: str | None = None
) -> int:
    """The account's checks today: live extraction calls, each job once, whatever their outcome."""
    cur = await conn.execute(
        "SELECT count(DISTINCT job_id) FROM gateway.llm_calls"
        f" WHERE user_id = %s AND stage = %s AND lane = 'live' AND created_at >= {DAY_START}"
        " AND job_id IS DISTINCT FROM %s",
        (user_id, CHECK_STAGE, other_than),
    )
    return (await cur.fetchone())[0]


async def check_quota(
    conn: AsyncConnection, user_id: int | None, job_id: str | None, config: dict
) -> None:
    """Raises QuotaReached when one more check would pass the account's quota; a counted job passes."""
    quota = quota_for(user_id, config)
    if quota is None:
        return
    used = await checks_today(conn, user_id, other_than=job_id)
    if used >= quota:
        raise QuotaReached(user_id, used, quota)
