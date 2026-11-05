"""P3.06, P4.03 and P4.04 (D-054): deleting an account end to end, the retention purges, the privacy
page and its data inventory, feedback on results, and time to result.

web, core-api and the gateway run as ASGI apps on one migrated test database, wired as on the internal
network: web reaches core through its CoreClient, core reaches the gateway through its GatewayClient.
"""

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from procrastinate import App
from procrastinate.testing import InMemoryConnector
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from scc_common import periodic
from scc_core import api as core_api
from scc_core import config as core_config
from scc_core import jobs, tasks
from scc_core import privacy as core_privacy
from scc_core.gateway_client import GatewayClient
from scc_core.models import Claim, JobCreate, JobState
from scc_gateway import config as gateway_config
from scc_gateway import main as gateway_main
from scc_gateway import privacy as gateway_privacy
from scc_web import config, erasure
from scc_web.admin_client import AdminClient
from scc_web.auth import passwords, store
from scc_web.context import Deps
from scc_web.core_client import CoreClient, CoreError
from scc_web.main import build_app
from scc_web.security import PRE_CSRF_COOKIE, SESSION_COOKIE
from tests.helpers import run_async
from tests.web_samples import CLAIM, RESULT

ROOT = Path(__file__).resolve().parents[2]
CFG = config.load()
PASSWORD = "a long test password"
ADMIN_HOST = "admin.test"


class Switch(httpx.AsyncBaseTransport):
    """The gateway as core sees it, with a switch to take it down and bring it back."""

    def __init__(self, app):
        self.inner, self.down = httpx.ASGITransport(app=app), False

    async def handle_async_request(self, request):
        if self.down:
            raise httpx.ConnectError("refused", request=request)
        return await self.inner.handle_async_request(request)


def with_stack(migrated_db, steps, *, operator=None):
    async def scenario():
        pools = {
            m: AsyncConnectionPool(migrated_db.conninfo(m), open=False)
            for m in ("web", "core", "gateway")
        }
        for pool in pools.values():
            await pool.open()
        queue = App(connector=InMemoryConnector())
        queue.add_tasks_from(tasks.blueprint, namespace="core")
        gateway = gateway_main.build_app(
            checks={},
            deps=gateway_main.Deps(pools["gateway"], None, None, gateway_config.load()),
        )
        switch = Switch(gateway)
        core = core_api.build_app(
            checks={},
            deps=core_api.Deps(
                pools["core"],
                queue,
                "pipeline-v8",
                gateway=GatewayClient("http://gateway", transport=switch),
            ),
        )
        core_transport = httpx.ASGITransport(app=core)
        deps = Deps(
            pools["web"],
            CoreClient("http://core", transport=core_transport),
            CFG,
            admin=AdminClient(
                "http://core",
                "http://gateway",
                core_transport,
                httpx.ASGITransport(app=gateway),
            ),
            admin_host=ADMIN_HOST,
            operator=operator,
        )
        web = build_app({}, deps=deps)
        ctx = {"pools": pools, "deps": deps, "gateway": switch}
        try:
            async with pools["web"].connection() as conn:
                for table in ("sessions", "login_attempts", "users"):
                    await conn.execute(f"DELETE FROM auth.{table}")
                for name, email, role in (
                    ("admin", "owner@example.com", "admin"),
                    ("user", "tester@example.com", "user"),
                    ("other", "other@example.com", "user"),
                ):
                    ctx[name] = await store.create_user(
                        conn, email, passwords.hash_password(PASSWORD), role
                    )
            async with pools["core"].connection() as conn:
                await conn.execute("DELETE FROM core.jobs")
                await conn.execute("DELETE FROM core.injection_events")
            async with pools["gateway"].connection() as conn:
                await conn.execute("DELETE FROM gateway.llm_calls")
                await conn.execute("DELETE FROM gateway.erased_users")

            def client(host="web"):
                return httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=web), base_url=f"https://{host}"
                )

            ctx["client"] = client
            async with queue.open_async():
                return await steps(ctx)
        finally:
            for pool in pools.values():
                await pool.close()

    return run_async(scenario())


def token(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


async def sign_in(client, email="tester@example.com"):
    csrf = token((await client.get("/sign-in")).text)
    return await client.post(
        "/sign-in",
        data={
            "email": email,
            "password": PASSWORD,
            "csrf_token": csrf,
            "next": "/history",
        },
    )


async def seed_check(pools, user_id, *, days_ago=0, state="done", result=None):
    """A check with everything that hangs off it, and its LLM call in the gateway, as the pipeline
    leaves them. Submitted `days_ago`, confirmed 20 s later, finished 65 s after submitting."""
    job_id = uuid.uuid4()
    at = datetime.now(UTC) - timedelta(days=days_ago)
    done = state == "done"
    async with pools["core"].connection() as conn:
        await conn.execute(
            "INSERT INTO core.jobs (id, user_id, idempotency_key, request_hash, state, input_type,"
            " input_text, confirmed_claim, result, config_version, created_at, updated_at,"
            " confirmed_at, finished_at) VALUES (%s, %s, %s, 'h', %s, 'text', %s, %s, %s,"
            " 'pipeline-v8', %s, %s, %s, %s)",
            (job_id, user_id, f"key-{job_id}", state, "Zinc shortens colds, my doctor says.",
             Jsonb(CLAIM), Jsonb(result or RESULT) if done else None, at, at,
             at + timedelta(seconds=20), at + timedelta(seconds=65) if done else None),
        )  # fmt: skip
        await conn.execute(
            "INSERT INTO core.job_events (job_id, state, at) VALUES (%s, 'submitted', %s)",
            (job_id, at),
        )
        await conn.execute(
            "INSERT INTO core.passages (job_id, pmcid, ordinal, section, text)"
            " VALUES (%s, 'PMC1', 0, 'Results', 'Zinc shortened colds.')",
            (job_id,),
        )
        await conn.execute(
            "INSERT INTO core.hallucination_events (job_id, stage, quote, reason, config_version)"
            " VALUES (%s, 'verify', 'made up', 'not_in_passage', 'pipeline-v8')",
            (job_id,),
        )
        await conn.execute(
            "INSERT INTO core.injection_events (job_id, user_id, stage, source, config_version)"
            " VALUES (%s, %s, 'extract', 'submitted_text', 'pipeline-v8')",
            (job_id, user_id),
        )
    async with pools["gateway"].connection() as conn:
        await conn.execute(
            "INSERT INTO gateway.llm_calls (lane, status, stage, job_id, user_id, model, cost_eur,"
            " output, created_at) VALUES ('live', 'ok', 'extract', %s, %s, 'claude-haiku-5-5',"
            " 0.001, %s, %s)",
            (str(job_id), user_id, Jsonb({"claims": [{"supplement": "zinc"}]}), at),
        )
    return job_id


async def rows(pools, job_ids) -> dict:
    """How many rows each table holds for these checks."""
    ids = [str(j) for j in job_ids]
    found = {}
    async with pools["core"].connection() as conn:
        for table in ("jobs", "job_events", "passages", "hallucination_events",
                      "injection_events", "feedback"):  # fmt: skip
            column = "id" if table == "jobs" else "job_id"
            cur = await conn.execute(
                f"SELECT count(*) FROM core.{table} WHERE {column}::text = ANY(%s)",
                (ids,),
            )
            found[table] = (await cur.fetchone())[0]
    async with pools["gateway"].connection() as conn:
        cur = await conn.execute(
            "SELECT count(*), count(user_id), count(output) FROM gateway.llm_calls"
            " WHERE job_id = ANY(%s)",
            (ids,),
        )
        found["llm_calls"], found["linked"], found["answers"] = await cur.fetchone()
    return found


async def account(pools, email) -> dict | None:
    async with pools["web"].connection() as conn:
        cur = await conn.execute(
            "SELECT id, disabled_at, deletion_requested_at,"
            " (SELECT count(*) FROM auth.sessions s WHERE s.user_id = u.id),"
            " (SELECT count(*) FROM auth.login_attempts a WHERE a.email_lower = lower(u.email))"
            " FROM auth.users u WHERE lower(email) = lower(%s)",
            (email,),
        )
        row = await cur.fetchone()
        attempts = await conn.execute(
            "SELECT count(*) FROM auth.login_attempts WHERE email_lower = lower(%s)",
            (email,),
        )
        left = (await attempts.fetchone())[0]
    if row is None:
        return {"exists": False, "attempts": left}
    return {
        "exists": True,
        "disabled": row[1] is not None,
        "deleting": row[2] is not None,
        "sessions": row[3],
        "attempts": left,
    }


# --- deleting an account (P3.06) ----------------------------------------------------------------


def test_deleting_an_account_removes_it_and_everything_it_made(migrated_db):
    async def steps(ctx):
        pools = ctx["pools"]
        mine = [await seed_check(pools, ctx["user"]) for _ in range(2)]
        theirs = await seed_check(pools, ctx["other"])
        async with ctx["client"]() as client:
            assert (await sign_in(client)).status_code == 303
            page = await client.get("/account")
            assert "Delete your account" in page.text
            csrf = token(page.text)
            wrong = await client.post(
                "/account/delete",
                data={
                    "csrf_token": csrf,
                    "password": "not my password",
                    "confirm": "yes",
                },
            )
            unticked = await client.post(
                "/account/delete", data={"csrf_token": csrf, "password": PASSWORD}
            )
            untouched = await rows(pools, mine)
            deleted = await client.post(
                "/account/delete",
                data={"csrf_token": csrf, "password": PASSWORD, "confirm": "yes"},
            )
            after = await client.get("/history")
        return {
            "wrong": wrong,
            "unticked": unticked,
            "untouched": untouched,
            "deleted": deleted,
            "after": after.status_code,
            "mine": await rows(pools, mine),
            "theirs": await rows(pools, [theirs]),
            "account": await account(pools, "tester@example.com"),
            "other": await account(pools, "other@example.com"),
        }

    r = with_stack(migrated_db, steps)
    assert (
        r["wrong"].status_code == 400 and "Your account is unchanged" in r["wrong"].text
    )
    assert r["unticked"].status_code == 400 and "Tick the box" in r["unticked"].text
    assert r["untouched"]["jobs"] == 2
    assert r["deleted"].status_code == 200
    assert "Your account is deleted" in r["deleted"].text
    assert f'{SESSION_COOKIE}=""' in r["deleted"].headers["set-cookie"]
    assert r["after"] == 303  # signed out
    # Web: the account, its sessions and its sign-in attempts are gone.
    assert r["account"] == {"exists": False, "attempts": 0}
    # Core: every check with everything attached to it.
    assert r["mine"] == {
        "jobs": 0, "job_events": 0, "passages": 0, "hallucination_events": 0,
        "injection_events": 0, "feedback": 0,
        # Gateway: the cost rows stay, unlinked, without their stored answers.
        "llm_calls": 2, "linked": 0, "answers": 0,
    }  # fmt: skip
    # Nobody else's data moves.
    assert r["theirs"]["jobs"] == 1 and r["theirs"]["linked"] == 1
    assert r["other"]["exists"]


def test_a_deletion_the_gateway_missed_is_finished_by_the_purge(migrated_db):
    async def steps(ctx):
        pools = ctx["pools"]
        mine = await seed_check(pools, ctx["user"])
        ctx["gateway"].down = True
        async with ctx["client"]() as client:
            await sign_in(client)
            csrf = token((await client.get("/account")).text)
            closed = await client.post(
                "/account/delete",
                data={"csrf_token": csrf, "password": PASSWORD, "confirm": "yes"},
            )
            signed_out = (await client.get("/history")).status_code
            refused = (await sign_in(client)).status_code
        waiting = await account(pools, "tester@example.com")
        half = await rows(pools, [mine])
        while_down = await erasure.purge(ctx["deps"])
        ctx["gateway"].down = False
        finished = await erasure.purge(ctx["deps"])
        return (
            closed,
            signed_out,
            refused,
            waiting,
            half,
            while_down,
            finished,
            (
                await account(pools, "tester@example.com"),
                await rows(pools, [mine]),
            ),
        )

    closed, signed_out, refused, waiting, half, while_down, finished, (gone, after) = (
        with_stack(migrated_db, steps)
    )
    assert closed.status_code == 200 and "Your account is closed" in closed.text
    assert signed_out == 303 and refused == 400
    # Closed at once: disabled, marked, signed out everywhere.
    assert waiting["exists"] and waiting["disabled"] and waiting["deleting"]
    assert waiting["sessions"] == 0
    # Core deleted the checks before it asked the gateway; the gateway's link is still there.
    assert half["jobs"] == 0 and half["linked"] == 1
    assert (
        while_down["deletions_waiting"] == 1 and while_down["deletions_finished"] == 0
    )
    assert finished["deletions_finished"] == 1 and finished["deletions_waiting"] == 0
    assert not gone["exists"]
    assert after["linked"] == 0 and after["answers"] == 0


def test_an_admin_deletes_an_account_for_someone_who_asked(migrated_db):
    async def steps(ctx):
        pools = ctx["pools"]
        mine = await seed_check(pools, ctx["user"])
        async with ctx["client"](ADMIN_HOST) as client:
            await sign_in(client, "owner@example.com")
            page = await client.get("/admin/users")
            csrf = token(page.text)
            mistyped = await client.post(
                f"/admin/users/{ctx['user']}/delete",
                data={"csrf_token": csrf, "confirm_email": "someone@example.com"},
            )
            self_delete = await client.post(
                f"/admin/users/{ctx['admin']}/delete",
                data={"csrf_token": csrf, "confirm_email": "owner@example.com"},
            )
            deleted = await client.post(
                f"/admin/users/{ctx['user']}/delete",
                data={"csrf_token": csrf, "confirm_email": "Tester@Example.com"},
            )
        return (
            page,
            mistyped,
            self_delete,
            deleted,
            (
                await account(pools, "tester@example.com"),
                await rows(pools, [mine]),
            ),
        )

    page, mistyped, self_delete, deleted, (gone, after) = with_stack(migrated_db, steps)
    assert "Type their email to confirm" in page.text
    assert mistyped.status_code == 400 and "Type tester@example.com" in mistyped.text
    assert self_delete.status_code == 400 and "under Account" in self_delete.text
    assert "tester@example.com is deleted, with all their checks." in deleted.text
    assert not gone["exists"] and after["jobs"] == 0 and after["linked"] == 0


# --- retention purges (P3.06) -------------------------------------------------------------------


def test_web_purge_deletes_old_sign_in_attempts_and_ended_sessions(migrated_db):
    async def steps(ctx):
        now = datetime.now(UTC)
        async with ctx["pools"]["web"].connection() as conn:
            for days in (31, 29):
                await store.record_attempt(
                    conn,
                    "tester@example.com",
                    "203.0.113.9",
                    True,
                    now - timedelta(days=days),
                )
            # One session past its absolute limit, one idle too long, one live.
            await store.create_session(
                conn, ctx["user"], now - timedelta(hours=13), CFG
            )
            await store.create_session(conn, ctx["user"], now - timedelta(hours=2), CFG)
            await store.create_session(conn, ctx["user"], now, CFG)
            return await store.purge(conn, now, CFG)

    counts = with_stack(migrated_db, steps)
    assert counts == {"sign_in_attempts_deleted": 1, "sessions_deleted": 2}


def test_core_purge_deletes_old_checks_and_finished_queue_entries(migrated_db):
    async def steps(ctx):
        pools = ctx["pools"]
        old = await seed_check(pools, ctx["user"], days_ago=181)
        recent = await seed_check(pools, ctx["user"], days_ago=179)
        async with pools["core"].connection() as conn:
            await conn.execute("DELETE FROM core.procrastinate_periodic_defers")
            await conn.execute("DELETE FROM core.procrastinate_jobs")
            tasks_ = {}
            for name, status_, days in (("old_done", "succeeded", 31), ("new_done", "succeeded", 1),
                                        ("old_waiting", "todo", 31)):  # fmt: skip
                cur = await conn.execute(
                    "INSERT INTO core.procrastinate_jobs (queue_name, task_name, args, status)"
                    " VALUES ('pipeline', 'core:run_pipeline', '{}', %s) RETURNING id",
                    (status_,),
                )
                tasks_[name] = (await cur.fetchone())[0]
                await conn.execute(
                    "INSERT INTO core.procrastinate_events (job_id, type, at)"
                    " VALUES (%s, 'deferred', now() - make_interval(days => %s))",
                    (tasks_[name], days),
                )
                await conn.execute(
                    "UPDATE core.procrastinate_events SET at = now() - make_interval(days => %s)"
                    " WHERE job_id = %s",
                    (days, tasks_[name]),
                )
            without = await core_privacy.purge(conn, core_config.load("pipeline-v7"))
            counts = await core_privacy.purge(conn, core_config.load("pipeline-v8"))
            cur = await conn.execute(
                "SELECT id FROM core.procrastinate_jobs ORDER BY id"
            )
            left = {row[0] for row in await cur.fetchall()}
        return (
            without,
            counts,
            await rows(pools, [old]),
            await rows(pools, [recent]),
            (left == {tasks_["new_done"], tasks_["old_waiting"]}),
        )

    without, counts, old, recent, queue_ok = with_stack(migrated_db, steps)
    assert without is None  # a config without [retention] purges nothing
    assert counts == {"checks_deleted": 1, "tasks_deleted": 1}
    assert old["jobs"] == old["job_events"] == old["passages"] == 0
    assert recent["jobs"] == 1
    assert queue_ok


def test_gateway_erase_and_purge(migrated_db):
    async def steps(ctx):
        pool = ctx["pools"]["gateway"]

        async def call(user_id, days, output=True):
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "INSERT INTO gateway.llm_calls (lane, status, stage, user_id, model, cost_eur,"
                    " output, created_at) VALUES ('live', 'ok', 'assess', %s, 'claude-haiku-5-5',"
                    " 0.001, %s, now() - make_interval(days => %s)) RETURNING id",
                    (user_id, Jsonb({"a": 1}) if output else None, days),
                )
                return (await cur.fetchone())[0]

        ids = {
            "answer_8d": await call(5, 8),
            "answer_6d": await call(5, 6),
            "link_181d": await call(5, 181, output=False),
            "erased": await call(9, 1),
        }
        async with pool.connection() as conn:
            erased = await gateway_privacy.erase_user(conn, 9)
            again = await gateway_privacy.erase_user(conn, 9)
            # A call for the deleted account that was still running when it was erased.
            await conn.execute(
                "INSERT INTO gateway.llm_calls (lane, status, stage, user_id, model, cost_eur, output)"
                " VALUES ('live', 'ok', 'assess', 9, 'claude-haiku-5-5', 0.001, '{}')"
            )
            counts = await gateway_privacy.purge(conn, gateway_config.load())
            cur = await conn.execute(
                "SELECT id, user_id, output IS NOT NULL FROM gateway.llm_calls ORDER BY id"
            )
            left = {row[0]: (row[1], row[2]) for row in await cur.fetchall()}
        return erased, again, counts, ids, left

    erased, again, counts, ids, left = with_stack(migrated_db, steps)
    assert (erased, again) == (1, 0)
    assert counts == {
        "answers_cleared": 1,
        "links_removed": 1,
        "late_calls_unlinked": 1,
    }
    assert left[ids["answer_8d"]] == (5, False)
    assert left[ids["answer_6d"]] == (5, True)
    assert left[ids["link_181d"]] == (None, False)
    assert all(user != 9 for user, _ in left.values())
    assert len(left) == 5  # cost rows are never deleted


def test_a_periodic_task_runs_once_at_a_time_and_survives_failures(migrated_db):
    async def steps(ctx):
        pool = ctx["pools"]["web"]
        started, release = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            await release.wait()

        async def never():
            raise AssertionError("ran while another copy held the lock")

        first = asyncio.create_task(periodic.run_once(pool, "t-lock", slow))
        await started.wait()
        second = await periodic.run_once(pool, "t-lock", never)
        release.set()
        runs = []

        async def flaky():
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("first run fails")

        async with periodic.every(pool, "t-loop", flaky, seconds=0.01, first_delay=0):
            for _ in range(200):
                if len(runs) >= 3:
                    break
                await asyncio.sleep(0.01)
        return await first, second, len(runs)

    first, second, runs = with_stack(migrated_db, steps)
    assert first is True and second is False
    assert runs >= 3  # the failed first run didn't stop the loop


# --- the privacy page and the data inventory (P3.06) ---------------------------------------------


def test_the_privacy_page_is_public_and_states_the_retention(migrated_db):
    async def steps(ctx):
        async with ctx["client"]() as client:
            public = await client.get("/privacy")
            sign_in_page = await client.get("/sign-in")
        return public, sign_in_page

    def named(ctx_steps):
        async def steps(ctx):
            async with ctx["client"]() as client:
                return await client.get("/privacy")

        return steps

    public, sign_in_page = with_stack(migrated_db, steps)
    p = CFG["privacy"]
    assert public.status_code == 200
    assert "Sign out" not in public.text  # signed out: no account links in the header
    text = re.sub(r"<[^>]+>", "", public.text)
    assert f"{p['check_days']} days after you submit a check" in text
    assert f"{CFG['retention']['login_attempts_days']} days." in text
    assert f"after {p['llm_link_days']} days" in text
    assert "contact@suppclaimcheck.com" in text
    assert "run by the person who invited you" in text
    assert "isn't used to train" in text.replace("isn&#39;t", "isn't")
    assert 'href="/privacy"' in sign_in_page.text
    page = with_stack(migrated_db, named(None), operator="Example Operator")
    assert "run by Example Operator" in page.text


def test_only_the_two_sign_in_cookies_are_ever_set(migrated_db):
    async def steps(ctx):
        names = set()
        async with ctx["client"]() as client:
            responses = [await client.get("/privacy"), await client.get("/sign-in")]
            responses.append(await sign_in(client))
            for path in ("/history", "/account", "/checks/new", "/privacy"):
                responses.append(await client.get(path))
        for response in responses:
            for header in response.headers.get_list("set-cookie"):
                names.add(header.split("=", 1)[0])
        return names

    assert with_stack(migrated_db, steps) <= {SESSION_COOKIE, PRE_CSRF_COOKIE}


def test_the_privacy_page_numbers_match_each_modules_active_config():
    p = CFG["privacy"]
    core = core_config.load(core_config.ACTIVE)
    gateway = gateway_config.load(gateway_config.ACTIVE)
    assert p["check_days"] == core["retention"]["check_days"]
    assert p["llm_link_days"] == gateway["retention"]["user_link_days"]
    assert p["llm_answer_days"] == gateway["idempotency"]["output_retention_days"]
    backup = (ROOT / "deploy" / "backup.sh").read_text(encoding="utf-8")
    assert f"--keep-monthly {p['backup_months']}" in backup


def test_every_table_is_in_the_data_inventory(migrated_db):
    inventory = (ROOT / "docs" / "PRIVACY.md").read_text(encoding="utf-8")
    with psycopg.connect(migrated_db.admin_conninfo) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables"
                " WHERE table_schema IN ('auth', 'core', 'gateway') AND table_type = 'BASE TABLE'"
                " ORDER BY 1"
            )
        ]
    assert len(tables) > 10
    missing = [t for t in tables if f"`{t}`" not in inventory]
    assert not missing, f"add these tables to docs/PRIVACY.md: {missing}"


# --- feedback (P4.03) and time to result (P4.04) --------------------------------------------------


def test_feedback_is_saved_on_your_own_result_only(migrated_db):
    async def steps(ctx):
        pools = ctx["pools"]
        mine = await seed_check(pools, ctx["user"])
        theirs = await seed_check(pools, ctx["other"])
        running = await seed_check(pools, ctx["user"], state="assessing")
        out = {}
        async with ctx["client"]() as client:
            await sign_in(client)
            page = await client.get(f"/checks/{mine}")
            out["form"] = "How did this check read to you?" in page.text
            out["took"] = 'took <span class="num">45</span> seconds' in page.text
            csrf = token(page.text)
            out["empty"] = await client.post(
                f"/checks/{mine}/feedback", data={"csrf_token": csrf, "missing": "  "}
            )
            out["saved"] = await client.post(
                f"/checks/{mine}/feedback",
                data={"csrf_token": csrf, "verdict_right": "no", "understandable": "partly",
                      "missing": "The trial in children I read about."},
            )  # fmt: skip
            out["after"] = await client.get(f"/checks/{mine}?feedback=saved")
            out["changed"] = await client.post(
                f"/checks/{mine}/feedback",
                data={"csrf_token": csrf, "verdict_right": "yes"},
            )
            out["theirs"] = await client.post(
                f"/checks/{theirs}/feedback",
                data={"csrf_token": csrf, "verdict_right": "yes"},
            )
            out["running"] = await client.post(
                f"/checks/{running}/feedback",
                data={"csrf_token": csrf, "verdict_right": "yes"},
            )
        # Core checks the account and the state itself, whatever web does.
        core = ctx["deps"].core
        for name, job_id, user in (("core_theirs", theirs, ctx["user"]),
                                   ("core_running", running, ctx["user"])):  # fmt: skip
            try:
                await core.save_feedback(str(job_id), user, {"verdict_right": "yes"})
            except CoreError as exc:
                out[name] = exc.status
        out["stored"] = (await core.get_job(str(mine)))["feedback"]
        out["rows"] = await rows(pools, [theirs])
        return out

    r = with_stack(migrated_db, steps)
    assert r["form"] and r["took"]
    assert (
        r["empty"].status_code == 400
        and "Answer at least one question" in r["empty"].text
    )
    assert r["saved"].status_code == 303
    assert r["saved"].headers["location"].endswith("?feedback=saved#feedback")
    assert "Your answers are saved" in r["after"].text
    assert 'value="no" checked' in r["after"].text
    assert "The trial in children I read about." in r["after"].text
    assert r["changed"].status_code == 303
    assert r["stored"]["verdict_right"] == "yes" and r["stored"]["missing"] is None
    assert r["theirs"].status_code == 404 and r["running"].status_code == 404
    assert r["core_theirs"] == 404 and r["core_running"] == 409
    assert r["rows"]["feedback"] == 0


def test_time_to_result_is_recorded_on_every_state_change(migrated_db):
    async def steps(ctx):
        seen = {}
        async with ctx["pools"]["core"].connection() as conn:
            job, _ = await jobs.create(
                conn, JobCreate(user_id=ctx["user"], input_text="Zinc shortens colds."),
                "key-timing-1", "pipeline-v8", None,
            )  # fmt: skip

            async def times(name):
                cur = await conn.execute(
                    "SELECT confirmed_at, finished_at FROM core.jobs WHERE id = %s",
                    (job.id,),
                )
                seen[name] = await cur.fetchone()

            await jobs.set_state(conn, job.id, JobState.awaiting_confirmation)
            await times("waiting")
            await jobs.confirm(
                conn, job.id, Claim(supplement="zinc", effect="shortens colds")
            )
            await times("confirmed")
            await jobs.set_state(
                conn, job.id, JobState.done, result={"verdict": "Supported"}
            )
            await times("done")
            # An admin retry sends it back to work: the finish time clears until it ends again.
            await conn.execute(
                "UPDATE core.jobs SET state = 'searching' WHERE id = %s", (job.id,)
            )
            await times("retried")
            await conn.execute(
                "UPDATE core.jobs SET updated_at = now() - interval '1 hour' WHERE id = %s",
                (job.id,),
            )
            await jobs.fail_stalled(conn, ["searching"], 20, "stalled", "Stopped.")
            await times("stalled")
        return seen

    seen = with_stack(migrated_db, steps)
    assert seen["waiting"] == (None, None)
    assert seen["confirmed"][0] is not None and seen["confirmed"][1] is None
    assert seen["done"][1] is not None
    assert seen["retried"][1] is None and seen["retried"][0] == seen["confirmed"][0]
    assert seen["stalled"][1] is not None


def test_outcomes_summarise_feedback_and_time_to_result(migrated_db):
    async def steps(ctx):
        pools = ctx["pools"]
        rated = [await seed_check(pools, ctx["user"]) for _ in range(3)]
        await seed_check(pools, ctx["other"], days_ago=40)  # outside the window
        await seed_check(
            pools,
            ctx["other"],
            result={**RESULT, "reused_from": {"job_id": str(rated[0])}},
        )
        async with pools["core"].connection() as conn:
            for job_id, right, understood, missing in (
                (rated[0], "yes", "yes", None),
                (rated[1], "no", "partly", "A newer review."),
                (rated[2], None, "yes", None),
            ):
                await conn.execute(
                    "INSERT INTO core.feedback (job_id, user_id, verdict_right, understandable, missing)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (job_id, ctx["user"], right, understood, missing),
                )
        summary = await ctx["deps"].admin.outcomes(days=30)
        async with ctx["client"](ADMIN_HOST) as client:
            await sign_in(client, "owner@example.com")
            page = await client.get("/admin/outcomes?days=30")
            overview = await client.get("/admin")
            trace = await client.get(f"/admin/jobs/{rated[1]}")
        return summary, page, overview, trace

    summary, page, overview, trace = with_stack(migrated_db, steps)
    assert (summary["results"], summary["rated"], summary["reused"]) == (4, 3, 1)
    answers = {
        q["question"]: {a["answer"]: a["n"] for a in q["answers"]}
        for q in summary["questions"]
    }
    assert answers == {
        "verdict_right": {"yes": 1, "no": 1, "unsure": 0},
        "understandable": {"yes": 2, "partly": 1, "no": 0},
    }
    assert [c["missing"] for c in summary["comments"]] == ["A newer review."]
    timing = {t["measure"]: t for t in summary["timings"]}
    # The reused result is left out: three checks, each 65 s from submitting, 45 s from confirming.
    assert timing["submit_to_result"]["checks"] == 3
    assert timing["submit_to_result"]["median_seconds"] == 65
    assert timing["confirm_to_result"]["p90_seconds"] == 45
    assert page.status_code == 200
    assert "A newer review." in page.text and "Time to result" in page.text
    assert 'aria-current="page">Last 30 days' in page.text
    assert "Feedback, last 30 days" in overview.text
    assert "The user's feedback" in trace.text and "Time to result" in trace.text
