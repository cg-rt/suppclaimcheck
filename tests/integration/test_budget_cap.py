"""P1.06 acceptance: the monthly cap refuses calls before they reach the provider, and fails closed."""

import copy

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from scc_gateway import budget
from scc_gateway import config as gateway_config
from scc_gateway.llm import LlmRequest, LlmResult, structured_call
from tests.helpers import run_async

REQUEST = LlmRequest(
    model="claude-haiku-5-5", max_tokens=1000, effort="low", thinking="disabled",
    instructions="Extract claims.", data="x" * 300, schema={"type": "object"},
)  # fmt: skip


class CountingProvider:
    def __init__(self):
        self.calls = 0

    async def complete_json(self, request):
        self.calls += 1
        return LlmResult(output={}, usage=budget.Usage(100, 50), model=request.model)


def call(pool_conninfo, cfg, provider):
    async def scenario():
        pool = AsyncConnectionPool(pool_conninfo, open=False, timeout=3)
        await pool.open(wait=False)
        try:
            return await structured_call(
                pool=pool,
                provider=provider,
                config=cfg,
                request=REQUEST,
                lane="live",
                stage="extract",
                job_id=None,
                user_id=1,
            )
        finally:
            await pool.close()

    return run_async(scenario())


def config_with_cap(cap_eur: float) -> dict:
    cfg = copy.deepcopy(gateway_config.load())
    cfg["budget"]["live_monthly_cap_eur"] = cap_eur
    return cfg


def test_call_under_the_cap_runs_and_is_recorded(migrated_db):
    provider = CountingProvider()
    _, call_id, cost = call(
        migrated_db.conninfo("gateway"), config_with_cap(1.0), provider
    )
    assert provider.calls == 1
    assert cost == pytest.approx((100 * 0.10 + 50 * 0.50) / 1_000_000)
    with psycopg.connect(migrated_db.admin_conninfo) as conn:
        status, tokens = conn.execute(
            "SELECT status, input_tokens FROM gateway.llm_calls WHERE id = %s",
            (call_id,),
        ).fetchone()
    assert (status, tokens) == ("ok", 100)


def test_cap_reached_refuses_before_the_provider_is_called(migrated_db):
    # The cap is set to EUR 0.0001 (as in the Phase 1 exit criteria); this call's worst case is more.
    provider = CountingProvider()
    with pytest.raises(budget.CapReached):
        call(migrated_db.conninfo("gateway"), config_with_cap(0.0001), provider)
    assert provider.calls == 0


def test_database_unreachable_fails_closed():
    provider = CountingProvider()
    unreachable = "host=127.0.0.1 port=1 user=nobody dbname=none connect_timeout=1"
    with pytest.raises(Exception):  # noqa: B017 - any error will do, as long as no call is made
        call(unreachable, config_with_cap(1.0), provider)
    assert provider.calls == 0
