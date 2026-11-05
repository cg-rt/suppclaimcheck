"""P3.04 and P3.05 in the gateway (D-053): warnings, levels, the daily quota, and idempotent LLM calls.

Every threshold is tested against a real, migrated database: call records are written straight into
`gateway.llm_calls` in the shapes the gateway leaves them, and the provider is a counting fake.
"""

import copy
import uuid

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from scc_gateway import budget
from scc_gateway import config as gateway_config
from scc_gateway import main as gateway_main
from scc_gateway.llm import (
    CallInProgress,
    LlmFailure,
    LlmRequest,
    LlmResult,
    structured_call,
)
from tests.helpers import run_async

REQUEST = LlmRequest(
    model="claude-haiku-5-5", max_tokens=100, effort="low", thinking="disabled",
    instructions="Extract claims.", data="x" * 30, schema={"type": "object"},
)  # fmt: skip


def cfg(cap: float = 1.0, quota: int = 2, exempt: list[int] | None = None) -> dict:
    c = copy.deepcopy(gateway_config.load())
    c["budget"]["live_monthly_cap_eur"] = cap
    c["quotas"] = {"checks_per_user_per_day": quota, "exempt_user_ids": exempt or []}
    return c


class Provider:
    def __init__(self, fail: str | None = None):
        self.calls, self.fail = 0, fail

    async def complete_json(self, request):
        self.calls += 1
        if self.fail:
            raise LlmFailure(self.fail, "unusable")
        return LlmResult(
            output={"n": self.calls}, usage=budget.Usage(10, 5), model=request.model
        )


def with_pool(migrated_db, steps):
    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("gateway"), open=False)
        await pool.open()
        try:
            async with pool.connection() as conn:
                await conn.execute("DELETE FROM gateway.llm_calls")
                await conn.execute("DELETE FROM gateway.budget_alerts")
            return await steps(pool)
        finally:
            await pool.close()

    return run_async(scenario())


async def spend(pool, eur: float, *, user_id=None, job_id=None, stage="assess", **row):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO gateway.llm_calls (lane, status, stage, job_id, user_id, model, cost_eur,"
            " idempotency_key, output, created_at) VALUES (%s, %s, %s, %s, %s, 'claude-haiku-5-5', %s,"
            " %s, %s, now() - make_interval(mins => %s))",
            (row.get("lane", "live"), row.get("status", "ok"), stage, job_id, user_id, eur,
             row.get("key"), row.get("output"), row.get("minutes_ago", 0)),
        )  # fmt: skip


def call(pool, provider, config, **kwargs):
    args = {"lane": "live", "stage": "assess", "job_id": "job-1", "user_id": 5} | kwargs
    return structured_call(
        pool=pool, provider=provider, config=config, request=REQUEST, **args
    )


def test_each_warning_level_is_sent_once_a_month(migrated_db):
    async def steps(pool):
        seen = []
        async with pool.connection() as conn:
            seen.append(await budget.warnings_due(conn, "live", cfg()))
            await spend(pool, 0.55)
            seen.append(await budget.warnings_due(conn, "live", cfg()))
            seen.append(await budget.warnings_due(conn, "live", cfg()))
            await spend(pool, 0.30)
            seen.append(await budget.warnings_due(conn, "live", cfg()))
            seen.append(await budget.warnings_due(conn, "eval", cfg()))
        return seen

    assert with_pool(migrated_db, steps) == [[], ["warn_50"], [], ["warn_80"], []]


@pytest.mark.parametrize(
    "spent, level",
    [(0.0, "ok"), (0.49, "ok"), (0.5, "warn_50"), (0.8, "warn_80"), (0.94, "warn_80"),
     (0.951, "cap"), (1.2, "cap")],
)  # fmt: skip
def test_levels_and_the_reserve_for_a_new_check(spent, level):
    # Cap 1.0 and a reserve of 0.05: a check that starts can finish, so "cap" comes just before it.
    assert budget.level("live", spent, cfg()) == level


def status(migrated_db, seed, config, user_id=None):
    async def steps(pool):
        await seed(pool)
        app = gateway_main.build_app(
            checks={}, deps=gateway_main.Deps(pool, Provider(), None, config)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            params = {"user_id": user_id} if user_id else {}
            return (await client.get("/budget/status", params=params)).json()

    return with_pool(migrated_db, steps)


def test_status_stops_new_checks_near_the_cap(migrated_db):
    async def near_cap(pool):
        await spend(pool, 0.97)

    body = status(migrated_db, near_cap, cfg())
    assert (body["new_checks_allowed"], body["code"]) == (False, "budget_cap_reached")
    assert [(lane["lane"], lane["level"]) for lane in body["lanes"]] == [
        ("live", "cap"),
        ("eval", "ok"),
    ]


def test_status_stops_an_account_at_its_daily_quota(migrated_db):
    async def two_checks(pool):
        for job in ("a", "b"):
            await spend(pool, 0.001, user_id=5, job_id=job, stage="extract")
            await spend(pool, 0.001, user_id=5, job_id=job, stage="assess")
        # Yesterday's check and another account's don't count.
        await spend(
            pool, 0.001, user_id=5, job_id="old", stage="extract", minutes_ago=60 * 25
        )
        await spend(pool, 0.001, user_id=6, job_id="c", stage="extract")

    body = status(migrated_db, two_checks, cfg(quota=2), user_id=5)
    assert (body["new_checks_allowed"], body["code"]) == (False, "daily_quota_reached")
    assert body["user"] == {"checks_today": 2, "quota": 2}
    other = status(migrated_db, two_checks, cfg(quota=2), user_id=6)
    assert other["new_checks_allowed"] is True and other["user"]["checks_today"] == 1
    exempt = status(migrated_db, two_checks, cfg(quota=2, exempt=[5]), user_id=5)
    assert exempt["new_checks_allowed"] is True and exempt["user"] is None


def test_extraction_past_the_quota_is_refused_before_the_provider(migrated_db):
    async def steps(pool):
        for job in ("a", "b"):
            await spend(pool, 0.001, user_id=5, job_id=job, stage="extract")
        provider = Provider()
        with pytest.raises(budget.QuotaReached):
            await call(pool, provider, cfg(quota=2), stage="extract", job_id="new")
        refused = provider.calls
        # A job already counted (an admin retry of its extraction) is not a new check.
        await call(pool, provider, cfg(quota=2), stage="extract", job_id="a")
        # Assessment calls of a check never count against the quota.
        await call(pool, provider, cfg(quota=2), stage="assess", job_id="new")
        return refused, provider.calls

    assert with_pool(migrated_db, steps) == (0, 2)


def test_the_same_key_returns_the_stored_answer_without_a_charge(migrated_db):
    async def steps(pool):
        provider = Provider()
        first = await call(
            pool, provider, cfg(), idempotency_key="job-1:assess:1:abcdef12"
        )
        again = await call(
            pool, provider, cfg(), idempotency_key="job-1:assess:1:abcdef12"
        )
        other = await call(
            pool, provider, cfg(), idempotency_key="job-1:assess:2:abcdef12"
        )
        async with pool.connection() as conn:
            rows = await (
                await conn.execute("SELECT count(*) FROM gateway.llm_calls")
            ).fetchone()
        return provider.calls, first, again, other, rows[0]

    calls, first, again, other, rows = with_pool(migrated_db, steps)
    assert calls == 2 and rows == 2  # the repeat made no call and no record
    assert (again[0].output, again[0].reused, again[1], again[2]) == (
        {"n": 1},
        True,
        first[1],
        0.0,
    )
    assert first[0].reused is False and first[2] > 0
    assert other[0].output == {"n": 2}


def test_a_failed_call_frees_its_key(migrated_db):
    async def steps(pool):
        with pytest.raises(LlmFailure):
            await call(
                pool,
                Provider(fail="output_not_json"),
                cfg(),
                idempotency_key="key-failed-1",
            )
        provider = Provider()
        result, _, _ = await call(pool, provider, cfg(), idempotency_key="key-failed-1")
        return provider.calls, result.reused

    assert with_pool(migrated_db, steps) == (1, False)


def test_a_running_call_holds_its_key_until_it_is_abandoned(migrated_db):
    async def steps(pool):
        await spend(pool, 0.01, status="reserved", key="key-running-1", job_id="job-1")
        with pytest.raises(CallInProgress):
            await call(pool, Provider(), cfg(), idempotency_key="key-running-1")
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE gateway.llm_calls SET created_at = now() - interval '11 minutes'"
            )
        provider = Provider()
        await call(pool, provider, cfg(), idempotency_key="key-running-1")
        async with pool.connection() as conn:
            old = await (
                await conn.execute(
                    "SELECT status, error FROM gateway.llm_calls ORDER BY id LIMIT 1"
                )
            ).fetchone()
        return provider.calls, old

    assert with_pool(migrated_db, steps) == (1, ("failed", "abandoned"))


def test_stored_answers_expire_and_the_key_calls_again(migrated_db):
    async def steps(pool):
        await spend(
            pool,
            0.01,
            key="key-old-1",
            output='{"n": 0}',
            job_id="job-1",
            minutes_ago=8 * 24 * 60,
        )
        provider = Provider()
        result, _, _ = await call(pool, provider, cfg(), idempotency_key="key-old-1")
        async with pool.connection() as conn:
            kept = await (
                await conn.execute(
                    "SELECT count(*) FROM gateway.llm_calls WHERE output IS NOT NULL"
                )
            ).fetchone()
        return provider.calls, result.output, kept[0]

    assert with_pool(migrated_db, steps) == (1, {"n": 1}, 1)


def test_the_endpoint_maps_quota_and_running_calls_and_checks_the_key(migrated_db):
    async def steps(pool):
        for job in ("a", "b"):
            await spend(pool, 0.001, user_id=5, job_id=job, stage="extract")
        await spend(pool, 0.01, status="reserved", key="key-running-2", job_id="job-1")
        app = gateway_main.build_app(
            checks={}, deps=gateway_main.Deps(pool, Provider(), None, cfg(quota=2))
        )
        body = {
            "stage": "extract", "job_id": "new", "user_id": 5, "model": "claude-haiku-5-5",
            "max_tokens": 100, "effort": "low", "thinking": "disabled",
            "instructions": "Extract.", "data": "text", "schema": {"type": "object"},
        }  # fmt: skip
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            quota = await client.post("/llm/structured", json=body)
            running = await client.post(
                "/llm/structured",
                json={**body, "stage": "assess"},
                headers={"Idempotency-Key": "key-running-2"},
            )
            bad_key = await client.post(
                "/llm/structured", json=body, headers={"Idempotency-Key": "a b"}
            )
            fresh = await client.post(
                "/llm/structured",
                json={**body, "stage": "assess", "job_id": str(uuid.uuid4())},
                headers={"Idempotency-Key": "key-fresh-12"},
            )
        return quota, running, bad_key, fresh

    quota, running, bad_key, fresh = with_pool(migrated_db, steps)
    assert (
        quota.status_code == 429
        and quota.json()["detail"]["code"] == "daily_quota_reached"
    )
    assert (
        running.status_code == 409
        and running.json()["detail"]["code"] == "call_in_progress"
    )
    assert bad_key.status_code == 422
    assert fresh.status_code == 200 and fresh.json()["reused"] is False


class Alerts:
    def __init__(self):
        self.sent = []

    async def send(self, event, text, code=None):
        self.sent.append((event, code))
        return "sent"


def test_a_call_that_passes_a_level_sends_its_warning_once(migrated_db):
    async def steps(pool):
        # Just under half the cap; the call itself (a few millionths of a euro) crosses it.
        await spend(pool, 0.499999)
        alerts = Alerts()
        deps = gateway_main.Deps(pool, Provider(), None, cfg(), alerts=alerts)
        app = gateway_main.build_app(checks={}, deps=deps)
        body = {
            "stage": "assess", "job_id": "job-9", "user_id": 5, "model": "claude-haiku-5-5",
            "max_tokens": 100, "effort": "low", "thinking": "disabled",
            "instructions": "Assess.", "data": "text", "schema": {"type": "object"},
        }  # fmt: skip
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            for _ in range(2):
                assert (
                    await client.post("/llm/structured", json=body)
                ).status_code == 200
        return alerts.sent

    assert with_pool(migrated_db, steps) == [("budget_warning", "live_warn_50")]
