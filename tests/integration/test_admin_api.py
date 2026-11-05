"""P3.08-P3.10: core's and the gateway's admin endpoints against a real, migrated database.

Jobs are written straight into core's tables in the shapes the pipeline leaves them, so each test
controls the states, events, passages and dropped quotes it reads back.
"""

import uuid
from datetime import datetime, timedelta

import httpx
import psycopg
from procrastinate import App
from procrastinate.testing import InMemoryConnector
from psycopg_pool import AsyncConnectionPool

from scc_core import api as core_api
from scc_core import config as core_config
from scc_core import jobs, tasks
from scc_gateway import config as gateway_config
from scc_gateway import main as gateway_main
from tests.admin_samples import seed, seed_safety
from tests.helpers import run_async


def with_core(migrated_db, steps, version="pipeline-v4"):
    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await pool.open()
        connector = InMemoryConnector()
        queue = App(connector=connector)
        queue.add_tasks_from(tasks.blueprint, namespace="core")
        app = core_api.build_app(checks={}, deps=core_api.Deps(pool, queue, version))
        try:
            async with pool.connection() as conn:
                ids = await seed(conn)
            async with (
                queue.open_async(),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://core"
                ) as client,
            ):
                return await steps(
                    client, {"ids": ids, "pool": pool, "queue": connector}
                )
        finally:
            await pool.close()

    return run_async(scenario())


def test_jobs_are_listed_by_status_with_counts_newest_first(migrated_db):
    async def steps(client, ctx):
        return (
            ctx["ids"],
            (await client.get("/admin/jobs")).json(),
            (await client.get("/admin/jobs", params={"status": "failed"})).json(),
            (await client.get("/admin/jobs", params={"status": "in_progress"})).json(),
        )

    ids, everything, failed, running = with_core(migrated_db, steps)
    assert everything["counts"] == {
        "in_progress": 1,
        "done": 1,
        "awaiting_confirmation": 1,
        "failed": 2,
        "refused": 0,
    }
    assert everything["jobs"][0]["id"] == str(ids["waiting"])
    assert [j["id"] for j in failed["jobs"]] == [
        str(ids["failed_early"]),
        str(ids["failed_late"]),
    ]
    assert [j["failure_code"] for j in failed["jobs"]] == [
        "invalid_output",
        "provider_unavailable",
    ]
    assert failed["counts"] == everything["counts"]
    assert [j["state"] for j in running["jobs"]] == ["assessing"]
    done = next(j for j in everything["jobs"] if j["status"] == "done")
    assert (done["verdict"], done["score"], done["supplement"]) == (
        "Supported",
        67,
        "creatine",
    )


def test_search_matches_ids_text_claims_and_users(migrated_db):
    async def steps(client, ctx):
        ids = ctx["ids"]

        async def found(**params):
            body = (await client.get("/admin/jobs", params=params)).json()
            return {j["id"] for j in body["jobs"]}, body

        return ids, {
            "uuid": await found(q=str(ids["stalled"])),
            "prefix": await found(q=str(ids["waiting"])[:8].upper()),
            "text": await found(q="ZINC"),
            "claim": await found(q="muscle strength"),
            "wildcard": await found(q="100%"),
            "user": await found(user_id=8),
            "page": await found(limit=2),
        }

    ids, r = with_core(migrated_db, steps)
    assert r["uuid"][0] == {str(ids["stalled"])}
    assert r["prefix"][0] == {str(ids["waiting"])}
    assert r["text"][0] == {str(ids["failed_early"])}
    assert r["claim"][0] == {
        str(ids["done"]),
        str(ids["failed_late"]),
        str(ids["stalled"]),
    }
    assert r["wildcard"][0] == set()  # % is a character to find, not a wildcard
    assert r["user"][0] == {str(ids["failed_late"])}
    assert r["user"][1]["counts"]["failed"] == 1
    assert len(r["page"][0]) == 2 and r["page"][1]["more"] is True


def test_trace_has_events_with_durations_passages_and_dropped_quotes(migrated_db):
    async def steps(client, ctx):
        ids = ctx["ids"]
        return (
            (await client.get(f"/admin/jobs/{ids['done']}/trace")).json(),
            (await client.get(f"/admin/jobs/{ids['failed_late']}/trace")).json(),
            (await client.get(f"/admin/jobs/{ids['stalled']}/trace")).json(),
            await client.get(f"/admin/jobs/{uuid.uuid4()}/trace"),
        )

    done, failed, stalled, missing = with_core(migrated_db, steps)
    assert [e["state"] for e in done["events"]][-1] == "done"
    assert done["events"][0]["seconds"] == 10 and done["events"][-1]["seconds"] is None
    assert done["job"]["result"]["verdict"] == "Supported"
    assert done["dropped_quotes"][0]["reason"] == "not_in_passage"
    assert (done["retryable"], done["stalled"]) == (False, False)
    assert failed["passages"] == [{"pmcid": "PMC6543210", "passages": 3}]
    assert (failed["failure_code"], failed["retryable"]) == (
        "provider_unavailable",
        True,
    )
    assert failed["job"]["correlation_id"].startswith("corr")
    assert (stalled["status"], stalled["stalled"]) == ("in_progress", True)
    assert stalled["events"][-1]["seconds"] >= 20 * 60
    assert missing.status_code == 404


def test_passages_of_one_article_in_reading_order(migrated_db):
    async def steps(client, ctx):
        job = ctx["ids"]["failed_late"]
        return (
            await client.get(
                f"/admin/jobs/{job}/passages", params={"pmcid": "PMC6543210"}
            ),
            await client.get(f"/admin/jobs/{job}/passages", params={"pmcid": "PMC1"}),
            await client.get(
                f"/admin/jobs/{job}/passages", params={"pmcid": "x' OR 1=1"}
            ),
        )

    found, other, bad = with_core(migrated_db, steps)
    assert [p["text"] for p in found.json()] == [
        "Passage 0.",
        "Passage 1.",
        "Passage 2.",
    ]
    assert other.status_code == 404
    assert bad.status_code == 422


def test_retry_sends_a_failed_job_back_to_the_phase_that_failed_once(migrated_db):
    async def steps(client, ctx):
        ids = ctx["ids"]
        late = await client.post(
            f"/admin/jobs/{ids['failed_late']}/retry", json={"actor_user_id": 1}
        )
        again = await client.post(
            f"/admin/jobs/{ids['failed_late']}/retry", json={"actor_user_id": 1}
        )
        early = await client.post(
            f"/admin/jobs/{ids['failed_early']}/retry", json={"actor_user_id": 1}
        )
        done = await client.post(
            f"/admin/jobs/{ids['done']}/retry", json={"actor_user_id": 1}
        )
        missing = await client.post(
            f"/admin/jobs/{uuid.uuid4()}/retry", json={"actor_user_id": 1}
        )
        async with ctx["pool"].connection() as conn:
            left = await (
                await conn.execute(
                    "SELECT count(*) FROM core.passages WHERE job_id = %s",
                    (ids["failed_late"],),
                )
            ).fetchone()
        trace = (await client.get(f"/admin/jobs/{ids['failed_late']}/trace")).json()
        queued = [(j["task_name"], j["args"]) for j in ctx["queue"].jobs.values()]
        return ids, late, again, early, done, missing, left[0], trace, queued

    ids, late, again, early, done, missing, left, trace, queued = with_core(
        migrated_db, steps
    )
    assert late.status_code == 200
    assert (late.json()["state"], late.json()["error"]) == ("searching", None)
    assert again.status_code == 409 and again.json()["detail"]["code"] == "wrong_state"
    assert early.json()["state"] == "submitted"
    assert done.status_code == 409 and missing.status_code == 404
    assert left == 0  # stored again by the search, so never ranked twice
    assert trace["events"][-1]["detail"] == "retry by admin user 1"
    assert queued == [
        (tasks.RUN, {"job_id": str(ids["failed_late"]), "correlation_id": trace["job"]["correlation_id"]}),
        (tasks.EXTRACT, {"job_id": str(ids["failed_early"]), "correlation_id": f"corr{ids['failed_early'].hex[:12]}"}),
    ]  # fmt: skip


def test_recent_errors_and_queue_status(migrated_db):
    async def steps(client, ctx):
        async with ctx["pool"].connection() as conn:
            await conn.execute(
                "INSERT INTO core.procrastinate_workers (last_heartbeat) VALUES (now() - interval '5 seconds')"
            )
        return (
            ctx["ids"],
            (await client.get("/admin/errors")).json(),
            (await client.get("/admin/errors", params={"limit": 1})).json(),
            (await client.get("/admin/queue")).json(),
        )

    ids, errors, one, queue = with_core(migrated_db, steps)
    kinds = {(e["kind"], e["job_id"], e["code"]) for e in errors}
    assert kinds == {
        ("job_failed", str(ids["failed_late"]), "provider_unavailable"),
        ("job_failed", str(ids["failed_early"]), "invalid_output"),
        ("job_stalled", str(ids["stalled"]), "assessing"),
        ("quote_dropped", str(ids["done"]), "not_in_passage"),
    }
    assert [e["at"] for e in errors] == sorted((e["at"] for e in errors), reverse=True)
    assert len(one) == 1
    assert len(queue["workers"]) == 1 and 4 <= queue["workers"][0]["seconds_since"] < 60


def test_config_versions_are_readable_from_core_and_the_gateway(migrated_db):
    async def scenario():
        pools = {
            m: AsyncConnectionPool(migrated_db.conninfo(m), open=False)
            for m in ("core", "gateway")
        }
        for pool in pools.values():
            await pool.open()
        apps = {
            "core": core_api.build_app(
                checks={},
                deps=core_api.Deps(pools["core"], App(connector=InMemoryConnector())),
            ),
            "gateway": gateway_main.build_app(
                checks={},
                deps=gateway_main.Deps(
                    pools["gateway"], None, None, gateway_config.load()
                ),
            ),
        }
        out = {}
        try:
            for name, app in apps.items():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://x"
                ) as client:
                    out[name] = (
                        (await client.get("/admin/config")).json(),
                        await client.get(
                            f"/admin/config/versions/{name == 'core' and 'pipeline-v4' or 'gateway-v0'}"
                        ),
                        await client.get("/admin/config/versions/pipeline-v99"),
                        await client.get("/admin/config/versions/Not_A_Name"),
                    )
        finally:
            for pool in pools.values():
                await pool.close()
        return out

    out = run_async(scenario())
    core, core_v4, missing, bad = out["core"]
    assert core["active"] == core_config.ACTIVE and core["evaluated"] is True
    assert [v["version"] for v in core["versions"]][:2] == [
        "pipeline-v0",
        "pipeline-v1",
    ]
    assert [v["version"] for v in core["versions"] if v["active"]] == [
        core_config.ACTIVE
    ]
    assert core["history"][0]["action"] == "activate"
    assert 'version = "pipeline-v4"' in core_v4.json()["body_toml"]
    assert core_v4.json()["evals"] == []
    assert missing.status_code == 404 and bad.status_code == 422
    gateway, gateway_v0, _, _ = out["gateway"]
    assert gateway["active"] == gateway_config.ACTIVE and gateway["evaluated"] is False
    assert gateway_v0.json()["body"]["budget"]["live_monthly_cap_eur"] == 15.0


def with_gateway(migrated_db, steps):
    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("gateway"), open=False)
        await pool.open()
        app = gateway_main.build_app(
            checks={}, deps=gateway_main.Deps(pool, None, None, gateway_config.load())
        )
        try:
            async with pool.connection() as conn:
                await conn.execute("DELETE FROM gateway.llm_calls")
                rows = [
                    ("live", "ok", "extract", "job-a", 100, 20, 0.0002, 900, None, 0),
                    ("live", "ok", "assess", "job-a", 4000, 300, 0.004, 3000, None, 0),
                    ("live", "failed", "assess", "job-b", None, None, 0.0, 120, "provider_unavailable", 0),
                    ("eval", "ok", "assess", None, 3000, 200, 0.003, 2500, None, 0),
                    ("live", "ok", "extract", "job-old", 100, 20, 0.5, 800, None, 70),
                ]  # fmt: skip
                for (
                    lane,
                    status,
                    stage,
                    job,
                    tin,
                    tout,
                    cost,
                    ms,
                    error,
                    days_ago,
                ) in rows:
                    await conn.execute(
                        "INSERT INTO gateway.llm_calls (lane, status, stage, job_id, user_id, model,"
                        " input_tokens, output_tokens, cost_eur, latency_ms, error, correlation_id,"
                        " created_at) VALUES (%s, %s, %s, %s, 7, 'claude-haiku-5-5', %s, %s, %s, %s,"
                        " %s, 'corr-1234', now() - make_interval(days => %s))",
                        (
                            lane,
                            status,
                            stage,
                            job,
                            tin,
                            tout,
                            cost,
                            ms,
                            error,
                            days_ago,
                        ),
                    )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                return await steps(client)
        finally:
            await pool.close()

    return run_async(scenario())


def test_llm_calls_of_one_job_oldest_first_and_failures(migrated_db):
    async def steps(client):
        return (
            (await client.get("/admin/llm-calls", params={"job_id": "job-a"})).json(),
            (await client.get("/admin/llm-calls", params={"status": "failed"})).json(),
            (await client.get("/admin/llm-calls", params={"lane": "nope"})),
        )

    job_a, failed, bad = with_gateway(migrated_db, steps)
    assert [c["stage"] for c in job_a] == ["extract", "assess"]
    assert job_a[1]["cost_eur"] == 0.004 and job_a[1]["latency_ms"] == 3000
    assert [(c["job_id"], c["error"]) for c in failed] == [
        ("job-b", "provider_unavailable")
    ]
    assert bad.status_code == 422


def test_usage_adds_up_this_month_per_lane_day_and_stage(migrated_db):
    async def steps(client):
        return (await client.get("/admin/usage")).json()

    report = with_gateway(migrated_db, steps)
    live, evals = report["lanes"]
    # The 70-day-old call is in neither this month nor the last 14 days.
    assert (live["lane"], live["calls"], live["failed"], live["cap_eur"]) == (
        "live",
        3,
        1,
        15.0,
    )
    assert round(live["spent_eur"], 6) == 0.0042
    assert (live["input_tokens"], live["output_tokens"]) == (4100, 320)
    assert live["jobs"] == 2  # job-a twice, job-b once
    assert (evals["lane"], evals["calls"]) == ("eval", 1)
    assert sum(d["calls"] for d in report["days"]) == 4
    assess = next(
        s for s in report["stages"] if (s["lane"], s["stage"]) == ("live", "assess")
    )
    assert (assess["calls"], assess["failed"], assess["median_latency_ms"]) == (
        2,
        1,
        1560.0,
    )


def test_a_job_stopped_to_clarify_counts_with_the_refused_ones(migrated_db):
    """P3.01's triage adds needs_clarification: stopped before search, like refused, never stalled."""
    with psycopg.connect(migrated_db.admin_conninfo, autocommit=True) as admin:
        # main's state check predates the state; P3.01's migration widens it.
        admin.execute(
            "ALTER TABLE core.jobs DROP CONSTRAINT IF EXISTS jobs_state_valid"
        )

    async def steps(client, ctx):
        async with ctx["pool"].connection() as conn:
            await conn.execute(
                "UPDATE core.jobs SET state = 'needs_clarification', updated_at = now() - interval '1 hour'"
                " WHERE id = %s",
                (ctx["ids"]["waiting"],),
            )
        return (
            ctx["ids"],
            (await client.get("/admin/jobs", params={"status": "refused"})).json(),
            (await client.get("/admin/errors")).json(),
        )

    ids, refused, errors = with_core(migrated_db, steps)
    assert (
        refused["counts"]["refused"] == 1
        and refused["counts"]["awaiting_confirmation"] == 0
    )
    assert [(j["id"], j["status"]) for j in refused["jobs"]] == [
        (str(ids["waiting"]), "refused")
    ]
    assert str(ids["waiting"]) not in {
        e["job_id"] for e in errors if e["kind"] == "job_stalled"
    }


async def _with_safety(ctx) -> dict:
    async with ctx["pool"].connection() as conn:
        return await seed_safety(conn)


def test_review_queue_lists_flagged_jobs_oldest_first_with_counts(migrated_db):
    """P3.03: jobs triage flagged wait in the queue, oldest first; reviewed ones newest first."""

    async def steps(client, ctx):
        safety = await _with_safety(ctx)
        return (
            safety,
            (await client.get("/admin/reviews")).json(),
            (await client.get("/admin/reviews", params={"status": "reviewed"})).json(),
            (await client.get("/admin/reviews", params={"limit": 1})).json(),
        )

    safety, waiting, reviewed, first = with_core(migrated_db, steps)
    assert waiting["counts"] == {"open": 3, "reviewed": 1}
    assert [j["id"] for j in waiting["jobs"]] == [
        str(safety["reflagged"]),
        str(safety["review_done"]),
        str(safety["review_waiting"]),
    ]
    by_id = {j["id"]: j for j in waiting["jobs"]}
    done = by_id[str(safety["review_done"])]
    assert (done["sensitive_topics"], done["verdict"], done["review"]) == (
        ["pregnancy"],
        "Supported",
        None,
    )
    # Not confirmed yet: the claim comes from extraction.
    assert by_id[str(safety["review_waiting"])]["supplement"] == "fish oil"
    # Flagged again after a review: back in the queue, the earlier review kept.
    assert by_id[str(safety["reflagged"])]["review"]["outcome"] == "notice_not_needed"
    assert [j["id"] for j in reviewed["jobs"]] == [str(safety["reviewed"])]
    assert reviewed["jobs"][0]["review"]["reviewed_by"] == 1
    assert first["more"] is True and len(first["jobs"]) == 1


def test_reviewing_takes_a_job_off_the_queue_once(migrated_db):
    async def steps(client, ctx):
        safety = await _with_safety(ctx)
        job = safety["review_done"]
        out = {
            "bad outcome": await client.post(
                f"/admin/jobs/{job}/review",
                json={"actor_user_id": 1, "outcome": "fine"},
            ),
            "long note": await client.post(
                f"/admin/jobs/{job}/review",
                json={"actor_user_id": 1, "outcome": "follow_up", "note": "x" * 501},
            ),
            "review": await client.post(
                f"/admin/jobs/{job}/review",
                json={
                    "actor_user_id": 1,
                    "outcome": "notice_right",
                    "note": "  Fits.  ",
                },
            ),
            "again": await client.post(
                f"/admin/jobs/{job}/review",
                json={"actor_user_id": 1, "outcome": "follow_up"},
            ),
            "never flagged": await client.post(
                f"/admin/jobs/{ctx['ids']['done']}/review",
                json={"actor_user_id": 1, "outcome": "notice_right"},
            ),
            "missing": await client.post(
                f"/admin/jobs/{uuid.uuid4()}/review",
                json={"actor_user_id": 1, "outcome": "notice_right"},
            ),
        }
        out["counts"] = (await client.get("/admin/reviews")).json()["counts"]
        # An edited claim that adds a topic flags the job again (pipeline.run._confirmed_topics).
        async with ctx["pool"].connection() as conn:
            await jobs.flag_review(conn, job, ["pregnancy", "children"])
        out["trace"] = (await client.get(f"/admin/jobs/{job}/trace")).json()
        return out

    out = with_core(migrated_db, steps)
    assert out["bad outcome"].status_code == 422
    assert out["long note"].status_code == 422
    review = out["review"].json()
    assert out["review"].status_code == 200 and review["needs_review"] is False
    assert (review["review"]["outcome"], review["review"]["note"]) == (
        "notice_right",
        "Fits.",
    )
    assert review["review"]["reviewed_by"] == 1
    assert out["again"].status_code == 409
    assert out["again"].json()["detail"]["code"] == "not_open"
    assert out["never flagged"].status_code == 409
    assert out["missing"].status_code == 404
    assert out["counts"] == {"open": 2, "reviewed": 2}
    job = out["trace"]["job"]
    assert job["needs_review"] is True and job["review"]["outcome"] == "notice_right"
    assert job["sensitive_topics"] == ["pregnancy", "children"]


def test_trace_carries_topics_and_the_jobs_injection_events(migrated_db):
    async def steps(client, ctx):
        safety = await _with_safety(ctx)
        return (
            (await client.get(f"/admin/jobs/{safety['review_done']}/trace")).json(),
            (await client.get(f"/admin/jobs/{ctx['ids']['done']}/trace")).json(),
        )

    flagged, plain = with_core(migrated_db, steps)
    assert flagged["job"]["sensitive_topics"] == ["pregnancy"]
    assert flagged["job"]["needs_review"] is True and flagged["job"]["review"] is None
    assert [(e["stage"], e["source"]) for e in flagged["injection_events"]] == [
        ("assess", "PMC6543210")
    ]
    assert plain["injection_events"] == [] and plain["job"]["sensitive_topics"] == []


def test_injection_report_shows_who_the_throttle_holds_and_until_when(migrated_db):
    """P3.02 in admin: per account, what the active config's throttle counts; every event below."""

    async def steps(client, ctx):
        safety = await _with_safety(ctx)
        return (
            safety,
            (await client.get("/admin/injections")).json(),
            (await client.get("/admin/injections", params={"limit": 2})).json(),
        )

    safety, report, page = with_core(migrated_db, steps, version="pipeline-v5")
    assert (report["config_version"], report["window_hours"], report["max_checks"]) == (
        "pipeline-v5",
        24,
        3,
    )
    # Only the user's own text counts: user 7's one event came from an article.
    [account] = report["accounts"]
    assert (
        account["user_id"],
        account["checks_in_window"],
        account["checks_total"],
    ) == (
        8,
        3,
        4,
    )
    assert account["throttled"] is True
    # Below the limit again when the oldest check in the window (9 hours ago) leaves it: in 15 hours.
    oldest = next(
        e for e in report["events"] if e["job_id"] == str(safety["injected"][2])
    )
    until = datetime.fromisoformat(account["throttled_until"])
    assert until - datetime.fromisoformat(oldest["at"]) == timedelta(hours=24)
    assert len(report["events"]) == 5
    assert report["events"][0]["source"] == "PMC6543210"  # the newest, 34 minutes ago
    assert [e["at"] for e in report["events"]] == sorted(
        (e["at"] for e in report["events"]), reverse=True
    )
    assert page["more"] is True and len(page["events"]) == 2


def test_without_a_throttle_in_the_config_no_account_is_held(migrated_db):
    async def steps(client, ctx):
        await _with_safety(ctx)
        return (await client.get("/admin/injections")).json()

    report = with_core(migrated_db, steps)  # pipeline-v4 has no guardrails
    assert (report["window_hours"], report["max_checks"]) == (None, None)
    assert [(a["throttled"], a["checks_total"]) for a in report["accounts"]] == [
        (None, 4)
    ]
