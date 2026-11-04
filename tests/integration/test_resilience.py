"""P3.04 and P3.05 in core (D-053): the budget check before a check starts, idempotent LLM calls,
partial failures, a repeated job step, result reuse and the sweep for stuck checks.

Core runs against a real, migrated database with a fake gateway (an httpx MockTransport) that answers
the way the real one does and records what core asked, headers included.
"""

import json
import re
import uuid
from datetime import UTC, datetime

import httpx
from procrastinate import App
from procrastinate.testing import InMemoryConnector
from psycopg_pool import AsyncConnectionPool

from scc_core import api as core_api
from scc_core import config as core_config
from scc_core import jobs, tasks
from scc_core.gateway_client import GatewayClient, idempotency_key
from scc_core.models import Claim, JobCreate
from scc_core.pipeline import run
from tests.helpers import run_async

FINDING = {
    "11": "Daily zinc lozenges shortened colds by two days compared with placebo.",
    "22": "Zinc acetate lozenges reduced the duration of common colds in adults.",
}
CLAIM = Claim(supplement="zinc", effect="shortens colds", population="adults")
# pipeline-v1's simple path (keyword retrieval, no triage) plus v7's cache and stall settings.
CONFIG = {
    **core_config.load("pipeline-v1"),
    "cache": {"result_days": 7},
    "resilience": {"stall_minutes": 20},
}


# --- the budget check before a check starts (P3.04) -------------------------------------------


def with_core(migrated_db, steps, budget):
    """core-api whose gateway answers /budget/status with `budget` (a dict, or an exception class)."""
    asked: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if isinstance(budget, type):
            raise budget("down", request=request)
        return httpx.Response(200, json=budget)

    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await pool.open()
        queue = App(connector=InMemoryConnector())
        queue.add_tasks_from(tasks.blueprint, namespace="core")
        deps = core_api.Deps(
            pool,
            queue,
            "pipeline-v1",
            gateway=GatewayClient(
                "http://gateway", transport=httpx.MockTransport(gateway)
            ),
        )
        app = core_api.build_app(checks={}, deps=deps)
        try:
            async with (
                queue.open_async(),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://core"
                ) as client,
            ):
                return await steps(client, pool), asked
        finally:
            await pool.close()

    return run_async(scenario())


def status(allowed=True, code=None, message=None):
    return {
        "new_checks_allowed": allowed,
        "code": code,
        "message": message,
        "lanes": [],
        "user": None,
    }


def new_job(client, key):
    return client.post(
        "/jobs",
        json={"user_id": 7, "input_text": "Zinc shortens colds."},
        headers={"Idempotency-Key": key},
    )


def test_a_new_check_is_refused_at_the_cap_or_the_daily_quota(migrated_db):
    async def steps(client, pool):
        return await new_job(client, f"budget-{uuid.uuid4()}")

    cap, asked = with_core(
        migrated_db, steps, status(False, "budget_cap_reached", "Budget used up.")
    )
    assert cap.status_code == 402
    assert cap.json()["detail"] == {
        "code": "budget_cap_reached",
        "message": "Budget used up.",
    }
    assert asked == ["http://gateway/budget/status?user_id=7"]
    quota, _ = with_core(
        migrated_db,
        steps,
        status(False, "daily_quota_reached", "Today's checks are used."),
    )
    assert (
        quota.status_code == 429
        and quota.json()["detail"]["code"] == "daily_quota_reached"
    )
    down, _ = with_core(migrated_db, steps, httpx.ConnectError)
    assert (
        down.status_code == 503
        and down.json()["detail"]["code"] == "budget_unavailable"
    )
    allowed, _ = with_core(migrated_db, steps, status())
    assert allowed.status_code == 201


def test_a_retry_of_an_existing_check_is_not_a_new_check(migrated_db):
    key = f"budget-{uuid.uuid4()}"

    async def steps(client, pool):
        async with pool.connection() as conn:
            await jobs.create(
                conn,
                JobCreate(user_id=7, input_text="Zinc shortens colds."),
                key,
                "pipeline-v1",
                None,
            )
        return await new_job(client, key)

    response, asked = with_core(
        migrated_db,
        steps,
        status(False, "daily_quota_reached", "Today's checks are used."),
    )
    assert response.status_code == 200 and asked == []


def test_a_confirmation_is_refused_at_the_cap_but_not_by_the_quota(migrated_db):
    async def steps(client, pool):
        async with pool.connection() as conn:
            job, _ = await jobs.create(
                conn,
                JobCreate(user_id=7, input_text="Zinc."),
                f"c-{uuid.uuid4()}",
                "pipeline-v1",
                None,
            )
            await conn.execute(
                "UPDATE core.jobs SET state = 'awaiting_confirmation' WHERE id = %s",
                (job.id,),
            )
        return await client.post(f"/jobs/{job.id}/confirm", json=CLAIM.model_dump())

    cap, asked = with_core(
        migrated_db, steps, status(False, "budget_cap_reached", "Used up.")
    )
    assert cap.status_code == 402 and asked == ["http://gateway/budget/status"]
    quota, _ = with_core(
        migrated_db, steps, status(False, "daily_quota_reached", "Today's.")
    )
    assert quota.status_code == 200 and quota.json()["state"] == "searching"


# --- idempotency keys (P3.05) ------------------------------------------------------------------


def test_the_idempotency_key_names_job_stage_attempt_and_request():
    body = {
        "job_id": str(uuid.uuid4()),
        "stage": "assess",
        "data": "passages",
        "lane": "live",
    }
    key = idempotency_key(body, 1)
    assert key == idempotency_key(
        dict(reversed(list(body.items()))), 1
    )  # order doesn't matter
    assert key != idempotency_key(body, 2)
    assert key != idempotency_key({**body, "data": "other passages"}, 1)
    assert key.startswith(f"{body['job_id']}:assess:1:")
    assert re.fullmatch(r"[A-Za-z0-9:_-]{8,128}", key)  # the gateway's header pattern


# --- the pipeline against a fake gateway ---------------------------------------------------------


class Gateway:
    """Answers search, fetch and assess calls; `fail_assess`, `fetch_failed` and `in_progress` script
    the failures. Records (path, body, idempotency key)."""

    def __init__(self, fail_assess=(), fetch_failed=(), in_progress=False):
        self.fail_assess, self.fetch_failed = set(fail_assess), set(fetch_failed)
        self.in_progress = in_progress
        self.calls: list[tuple[str, dict, str | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append(
            (request.url.path, body, request.headers.get("idempotency-key"))
        )
        path = request.url.path
        if path == "/notify":
            return httpx.Response(200, json={"outcome": "sent"})
        if path == "/literature/search":
            return httpx.Response(200, json={"count": 2, "pmids": ["11", "22"]})
        if path == "/literature/fetch":
            articles, texts, failed = [], {}, 0
            for pmid in body["pmids"]:
                a = {"pmid": pmid, "pmcid": f"PMC{pmid}", "title": f"Zinc study {pmid}",
                     "license": "cc by", "pub_types": ["Randomized Controlled Trial"], "mesh": ["Humans"]}  # fmt: skip
                if pmid in self.fetch_failed and body["full_texts"]:
                    a["skip_reason"], failed = "fetch_failed", failed + 1
                elif len(texts) < body["full_texts"]:
                    texts[a["pmcid"]] = (
                        f"<article><body><sec><title>Results</title><p>{FINDING[pmid]}</p></sec></body></article>"
                    )
                articles.append(a)
            counts = {"fetch_failed": failed} if failed else {}
            return httpx.Response(
                200, json={"articles": articles, "full_texts": texts, "counts": counts}
            )
        if path == "/llm/structured":
            if self.in_progress:
                return httpx.Response(
                    409, json={"detail": {"code": "call_in_progress", "message": ""}}
                )
            passages = json.loads(body["data"])["passages"]
            pmid = passages[0]["passage_id"].split("-")[0].removeprefix("PMC")
            if pmid in self.fail_assess:
                return httpx.Response(
                    503,
                    json={"detail": {"code": "provider_unavailable", "message": ""}},
                )
            out = [{"passage_id": p["passage_id"], "label": "supports", "quote": FINDING[pmid],
                    "reason": "Zinc shortened colds."} for p in passages]  # fmt: skip
            return httpx.Response(
                200,
                json={"output": {"assessments": out}, "model": "m", "input_tokens": 10,
                      "output_tokens": 5, "cost_eur": 0.0001, "call_id": len(self.calls)},
            )  # fmt: skip
        return httpx.Response(404)


def pipeline(migrated_db, gateway: Gateway, steps, config=CONFIG, use_cache=True):
    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await pool.open()
        rt = run.Runtime(
            pool,
            GatewayClient("http://gateway", transport=httpx.MockTransport(gateway)),
            config,
            use_cache=use_cache,
        )
        try:
            return await steps(rt, pool)
        finally:
            await pool.close()

    return run_async(scenario())


def unique_claim(**update) -> Claim:
    # The database is shared across tests and the result cache would reuse another test's check.
    return CLAIM.model_copy(update={"dose": f"{uuid.uuid4().hex[:8]} mg", **update})


async def confirmed_job(pool, claim=None, version=None):
    claim = claim or unique_claim()
    async with pool.connection() as conn:
        job, _ = await jobs.create(
            conn, JobCreate(user_id=7, input_text="Zinc shortens colds."),
            f"r-{uuid.uuid4()}", version or CONFIG["version"], None,
        )  # fmt: skip
        await conn.execute(
            "UPDATE core.jobs SET state = 'awaiting_confirmation' WHERE id = %s",
            (job.id,),
        )
        await jobs.confirm(conn, job.id, claim)
    return job.id


async def checked(rt, pool, **kwargs):
    job_id = await confirmed_job(pool, **kwargs)
    await run.run(rt, job_id)
    async with pool.connection() as conn:
        return await jobs.get(conn, job_id)


def test_llm_calls_carry_an_idempotency_key(migrated_db):
    gateway = Gateway()
    job = pipeline(migrated_db, gateway, lambda rt, pool: checked(rt, pool))
    keys = [key for path, _, key in gateway.calls if path == "/llm/structured"]
    assert len(keys) == 2 and all(k.startswith(f"{job.id}:assess:1:") for k in keys)
    assert (
        len(set(keys)) == 2
    )  # one per article: different passages, different requests


def test_an_article_the_model_did_not_answer_for_leaves_a_note(migrated_db):
    job = pipeline(
        migrated_db, Gateway(fail_assess={"22"}), lambda rt, pool: checked(rt, pool)
    )
    assert job.state == "done"
    assert [a["pmid"] for a in job.result["articles"]] == ["11"]
    assert job.result["limitations"][0] == run.UNANSWERED_NOTE.format(n=1)


def test_every_article_unanswered_fails_the_check(migrated_db):
    job = pipeline(
        migrated_db,
        Gateway(fail_assess={"11", "22"}),
        lambda rt, pool: checked(rt, pool),
    )
    assert (job.state, job.error) == ("failed", run.MESSAGES["provider_unavailable"])


def test_an_article_that_would_not_download_leaves_a_note(migrated_db):
    job = pipeline(
        migrated_db, Gateway(fetch_failed={"11"}), lambda rt, pool: checked(rt, pool)
    )
    assert job.state == "done" and [a["pmid"] for a in job.result["articles"]] == ["22"]
    assert job.result["limitations"][0] == run.NOT_DOWNLOADED_NOTE.format(n=1)
    assert job.result["searched"]["skipped"]["fetch_failed"] == 1


def test_nothing_downloaded_is_a_source_failure_not_an_answer(migrated_db):
    job = pipeline(
        migrated_db,
        Gateway(fetch_failed={"11", "22"}),
        lambda rt, pool: checked(rt, pool),
    )
    assert (job.state, job.error) == ("failed", run.MESSAGES["literature_unavailable"])


def test_a_job_step_already_running_elsewhere_stops_quietly(migrated_db):
    gateway = Gateway(in_progress=True)
    job = pipeline(migrated_db, gateway, lambda rt, pool: checked(rt, pool))
    # The other run owns the job: no failure, no alert; the state is where that run left it.
    assert job.state == "assessing" and job.error is None
    assert not [c for c in gateway.calls if c[0] == "/notify"]


def test_an_identical_recent_claim_reuses_the_result(migrated_db):
    gateway = Gateway()

    async def steps(rt, pool):
        claim = unique_claim()
        first = await checked(rt, pool, claim=claim)
        calls_after_first = len(gateway.calls)
        # Same claim, other case and spacing: reused, with no search and no LLM call.
        same = claim.model_copy(
            update={
                "supplement": "ZINC",
                "effect": "shortens  colds",
                "population": "Adults",
            }
        )
        second = await checked(rt, pool, claim=same)
        reused_calls = len(gateway.calls) - calls_after_first
        third = await checked(
            rt, pool, claim=same
        )  # reuses the first, never the reused second
        other_version = await checked(rt, pool, claim=same, version="pipeline-v1-other")
        return first, second, third, reused_calls, other_version

    first, second, third, reused_calls, other = pipeline(migrated_db, gateway, steps)
    assert first.result.get("reused_from") is None and first.result["claim_key"]
    assert reused_calls == 0
    assert second.result["reused_from"]["job_id"] == str(first.id)
    assert second.result["verdict"] == first.result["verdict"]
    assert second.result["limitations"][0].startswith(
        "This result was reused from an identical check on "
    )
    assert third.result["reused_from"]["job_id"] == str(first.id)
    assert (
        other.result.get("reused_from") is None
    )  # another config version is another method


def test_the_eval_runner_never_reuses(migrated_db):
    gateway = Gateway()

    async def steps(rt, pool):
        claim = unique_claim()
        await checked(rt, pool, claim=claim)
        before = len(gateway.calls)
        again = await checked(rt, pool, claim=claim)
        return again, len(gateway.calls) - before

    again, calls = pipeline(migrated_db, gateway, steps, use_cache=False)
    assert again.result.get("reused_from") is None and calls > 0


def test_the_sweep_ends_checks_stuck_in_a_working_state(migrated_db):
    gateway = Gateway()

    async def steps(rt, pool):
        ids = {}
        async with pool.connection() as conn:
            for name, state, minutes in (
                ("stuck", "assessing", 30), ("queued", "submitted", 25),
                ("busy", "assessing", 2), ("waiting", "awaiting_confirmation", 600),
            ):  # fmt: skip
                job, _ = await jobs.create(
                    conn,
                    JobCreate(user_id=7, input_text="Zinc."),
                    f"s-{uuid.uuid4()}",
                    "pipeline-v1",
                    None,
                )
                await conn.execute(
                    "UPDATE core.jobs SET state = %s, updated_at = now() - make_interval(mins => %s) WHERE id = %s",
                    (state, minutes, job.id),
                )
                ids[name] = job.id
        ended = await run.sweep_stalled(rt)
        none = await run.sweep_stalled(
            run.Runtime(pool, rt.gateway, core_config.load("pipeline-v1"))
        )
        async with pool.connection() as conn:
            states = {n: (await jobs.get(conn, i)).state for n, i in ids.items()}
            events = await jobs.events(conn, ids["stuck"])
        return ended, none, states, events

    ended, none, states, events = pipeline(migrated_db, gateway, steps)
    assert ended == 2 and none == 0
    assert states == {
        "stuck": "failed", "queued": "failed", "busy": "assessing", "waiting": "awaiting_confirmation",
    }  # fmt: skip
    assert (events[-1].state, events[-1].detail) == ("failed", "stalled")
    alerts = [body for path, body, _ in gateway.calls if path == "/notify"]
    assert [a["code"] for a in alerts] == ["stalled", "stalled"]
    assert datetime.now(UTC) >= events[-1].at
