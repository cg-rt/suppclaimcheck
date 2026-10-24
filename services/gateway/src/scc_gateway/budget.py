"""Cost of LLM calls and the monthly hard cap (P1.06, D-021). Fails closed.

Before each call, a row is reserved at the call's worst-case cost (estimated input, full max_tokens
output) under an advisory lock, so two calls at once can't both slip under the cap. After the call
the row gets the real tokens and cost. If the database can't be reached, no call is made.
"""

from dataclasses import dataclass

from psycopg import AsyncConnection


class UnknownModel(Exception):
    pass


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
        cur = await conn.execute(
            "SELECT coalesce(sum(cost_eur), 0) FROM gateway.llm_calls"
            " WHERE lane = %s AND created_at >= date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
            (lane,),
        )
        spent = float((await cur.fetchone())[0])
        if spent + reserve_eur > cap:
            raise CapReached(lane, spent, cap)
        cur = await conn.execute(
            """
            INSERT INTO gateway.llm_calls (lane, status, stage, job_id, user_id, model, cost_eur, correlation_id)
            VALUES (%s, 'reserved', %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                lane,
                record["stage"],
                record["job_id"],
                record["user_id"],
                record["model"],
                reserve_eur,
                record["correlation_id"],
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
) -> None:
    await conn.execute(
        """
        UPDATE gateway.llm_calls
        SET status = %s, input_tokens = %s, output_tokens = %s, cost_eur = %s, latency_ms = %s, error = %s
        WHERE id = %s
        """,
        (
            status,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            cost,
            latency_ms,
            error,
            call_id,
        ),
    )
