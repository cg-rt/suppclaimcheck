"""P1.05 acceptance: the same Idempotency-Key twice returns the same job; confirmation is guarded."""

import httpx
from procrastinate import App
from procrastinate.testing import InMemoryConnector
from psycopg_pool import AsyncConnectionPool

from scc_core import api as core_api
from scc_core import tasks
from tests.helpers import run_async

BODY = {"user_id": 7, "input_text": "Magnesium helps you sleep better."}
CLAIM = {
    "supplement": "magnesium",
    "effect": "better sleep",
    "population": None,
    "dose": None,
    "duration": None,
}


def with_core(migrated_db, steps):
    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await pool.open()
        connector = InMemoryConnector()
        queue = App(connector=connector)
        queue.add_tasks_from(tasks.blueprint, namespace="core")
        app = core_api.build_app(
            checks={}, deps=core_api.Deps(pool, queue, "pipeline-v0")
        )
        try:
            async with (
                queue.open_async(),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://core"
                ) as client,
            ):
                return await steps(client, connector)
        finally:
            await pool.close()

    return run_async(scenario())


def test_same_key_twice_returns_the_same_job_and_queues_once(migrated_db):
    async def steps(client, connector):
        first = await client.post(
            "/jobs", json=BODY, headers={"Idempotency-Key": "same-key-0001"}
        )
        second = await client.post(
            "/jobs", json=BODY, headers={"Idempotency-Key": "same-key-0001"}
        )
        return first, second, len(connector.jobs)

    first, second, queued = with_core(migrated_db, steps)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert queued == 1


def test_same_key_with_a_different_body_is_a_conflict(migrated_db):
    async def steps(client, connector):
        await client.post(
            "/jobs", json=BODY, headers={"Idempotency-Key": "reused-key-01"}
        )
        return await client.post(
            "/jobs",
            json={**BODY, "input_text": "Zinc cures colds."},
            headers={"Idempotency-Key": "reused-key-01"},
        )

    response = with_core(migrated_db, steps)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "idempotency_conflict"


def test_missing_key_is_rejected(migrated_db):
    async def steps(client, connector):
        return await client.post("/jobs", json=BODY)

    assert with_core(migrated_db, steps).status_code == 422


def test_confirm_only_when_waiting_for_confirmation(migrated_db):
    async def steps(client, connector):
        job = (
            await client.post(
                "/jobs", json=BODY, headers={"Idempotency-Key": "confirm-key-01"}
            )
        ).json()
        too_early = await client.post(f"/jobs/{job['id']}/confirm", json=CLAIM)
        unknown = await client.post(
            "/jobs/00000000-0000-0000-0000-000000000000/confirm", json=CLAIM
        )
        return too_early, unknown, len(connector.jobs)

    too_early, unknown, queued = with_core(migrated_db, steps)
    assert too_early.status_code == 409
    assert too_early.json()["detail"]["code"] == "wrong_state"
    assert unknown.status_code == 404
    assert queued == 1  # only the extraction; nothing was searched
