"""P1.04 and P1.10 acceptance against a real database: sign-in, lockout, session expiry, CSRF, ownership.

core-api is replaced by tests.web_samples.FakeCore; web's own `auth` schema is real and migrated.
"""

import io
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool

from scc_web import admin, config
from scc_web.auth import passwords, store
from scc_web.context import Deps
from scc_web.core_client import CoreClient
from scc_web.main import build_app
from scc_web.security import SESSION_COOKIE
from tests.helpers import run_async
from tests.web_samples import FakeCore, job

CFG = config.load()
PASSWORD = "a long test password"
T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def with_web(migrated_db, steps):
    """Runs `steps(client, ctx)` against web with a fresh pool, a fake core and a movable clock."""

    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("web"), open=False)
        await pool.open()
        core, clock = FakeCore(), Clock()
        deps = Deps(
            pool,
            CoreClient("http://core", transport=core.transport()),
            CFG,
            clock=clock,
        )
        app = build_app({}, deps=deps)
        ctx = {"pool": pool, "core": core, "clock": clock}
        try:
            async with pool.connection() as conn:
                # The app role can't TRUNCATE (least privilege), so each test empties the tables row by row.
                for table in ("sessions", "login_attempts", "users"):
                    await conn.execute(f"DELETE FROM auth.{table}")
                ctx["user"] = await store.create_user(
                    conn,
                    "tester@example.com",
                    passwords.hash_password(PASSWORD),
                    "user",
                )
                ctx["other"] = await store.create_user(
                    conn, "other@example.com", passwords.hash_password(PASSWORD), "user"
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://web"
            ) as client:
                return await steps(client, ctx)
        finally:
            await pool.close()

    return run_async(scenario())


def token(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


async def sign_in(client, email="tester@example.com", password=PASSWORD, csrf=None):
    if csrf is None:
        csrf = token((await client.get("/sign-in")).text)
    data = {
        "email": email,
        "password": password,
        "csrf_token": csrf,
        "next": "/history",
    }
    return await client.post("/sign-in", data=data)


def test_right_password_signs_in_with_a_host_cookie_and_wrong_one_does_not(migrated_db):
    async def steps(client, ctx):
        wrong = await sign_in(client, password="not the password")
        right = await sign_in(client)
        return wrong, right

    wrong, right = with_web(migrated_db, steps)
    assert wrong.status_code == 400
    assert "don&#39;t match an account" in wrong.text
    assert right.status_code == 303 and right.headers["location"] == "/history"
    cookie = right.headers["set-cookie"]
    assert (
        cookie.startswith(f"{SESSION_COOKIE}=")
        and "HttpOnly" in cookie
        and "Secure" in cookie
    )


def test_sign_in_without_the_csrf_token_is_rejected(migrated_db):
    async def steps(client, ctx):
        response = await sign_in(client, csrf="forged-token")
        async with ctx["pool"].connection() as conn:
            sessions = await (
                await conn.execute("SELECT count(*) FROM auth.sessions")
            ).fetchone()
        return response, sessions[0]

    response, sessions = with_web(migrated_db, steps)
    assert response.status_code == 400
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert sessions == 0


def test_five_failures_lock_the_account_even_for_the_right_password(migrated_db):
    async def steps(client, ctx):
        for _ in range(CFG["login"]["max_failures_per_account"]):
            await sign_in(client, password="wrong password!")
        locked = await sign_in(client)
        ctx["clock"].advance(minutes=CFG["login"]["window_minutes"] + 1)
        later = await sign_in(client)
        return locked, later

    locked, later = with_web(migrated_db, steps)
    assert locked.status_code == 429
    assert "Too many sign-in attempts" in locked.text
    assert later.status_code == 303


def test_one_address_is_rate_limited_across_accounts(migrated_db):
    async def steps(client, ctx):
        for i in range(CFG["login"]["max_attempts_per_address"]):
            await sign_in(client, email=f"guess{i}@example.com")
        return await sign_in(client)

    assert with_web(migrated_db, steps).status_code == 429


def test_sessions_end_after_the_idle_and_the_absolute_timeout(migrated_db):
    async def steps(client, ctx):
        await sign_in(client)
        clock = ctx["clock"]
        clock.advance(minutes=CFG["session"]["idle_minutes"] - 1)
        still = (await client.get("/history")).status_code
        clock.advance(minutes=CFG["session"]["idle_minutes"] + 1)
        idle = await client.get("/history")

        client.cookies.clear()
        await sign_in(client)
        # Active all along, but past the absolute limit.
        for _ in range(CFG["session"]["absolute_hours"] * 2):
            clock.advance(minutes=30)
            last = await client.get("/history")
        return still, idle, last

    still, idle, last = with_web(migrated_db, steps)
    assert still == 200
    assert idle.status_code == 303 and idle.headers["location"].startswith("/sign-in")
    assert last.status_code == 303


def test_sign_in_issues_a_new_session_id_and_drops_a_planted_one(migrated_db):
    async def steps(client, ctx):
        async with ctx["pool"].connection() as conn:
            planted, _ = await store.create_session(conn, ctx["other"], T0, CFG)
        # The form is fetched first; then an attacker's own session ID is planted before the POST.
        form = await client.get("/sign-in")
        client.cookies.set(SESSION_COOKIE, planted)
        response = await sign_in(client, csrf=token(form.text))
        async with ctx["pool"].connection() as conn:
            still_there = await store.load_session(conn, planted, T0, CFG)
        return response, planted, still_there

    response, planted, still_there = with_web(migrated_db, steps)
    assert response.status_code == 303
    assert planted not in response.headers["set-cookie"]
    assert still_there is None


def test_signed_in_forms_need_the_session_csrf_token(migrated_db):
    async def steps(client, ctx):
        await sign_in(client)
        forged = await client.post(
            "/checks", data={"input_text": "Zinc helps colds.", "csrf_token": "nope"}
        )
        page = await client.get("/checks/new")
        ok = await client.post(
            "/checks",
            data={
                "input_text": "Zinc helps colds.",
                "csrf_token": token(page.text),
                "idempotency_key": "11111111-2222-3333-4444-555555555555",
            },
        )
        return forged, ok, ctx["core"].calls

    forged, ok, calls = with_web(migrated_db, steps)
    assert forged.status_code == 400 and "This form expired" in forged.text
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1, "the forged form must not reach core"
    assert ok.status_code == 303 and ok.headers["location"].startswith("/checks/")


def test_a_check_is_only_visible_to_its_owner(migrated_db):
    async def steps(client, ctx):
        mine = ctx["core"].add(job(ctx["user"]))
        theirs = ctx["core"].add(job(ctx["other"]))
        await sign_in(client)
        return await client.get(f"/checks/{mine['id']}"), await client.get(
            f"/checks/{theirs['id']}"
        )

    mine, theirs = with_web(migrated_db, steps)
    assert mine.status_code == 200
    assert theirs.status_code == 404 and "Upper-body strength" not in theirs.text


def test_result_page_shows_verdict_quotes_citation_limits_and_the_notice(migrated_db):
    async def steps(client, ctx):
        done = ctx["core"].add(job(ctx["user"]))
        await sign_in(client)
        return (await client.get(f"/checks/{done['id']}")).text

    html = with_web(migrated_db, steps)
    for expected in (
        "Supported",
        ">67<",
        "Upper-body strength increased more",
        "CC BY",
        "PMID",
        "Not medical advice",
        "What was searched",
        "What could not be checked",
        "A review we could not read",
        "rubric-v0",
    ):
        assert expected in html, expected
    assert "proven" not in html.lower() and "cures" not in html.lower()


def test_each_state_gets_its_own_page(migrated_db):
    async def steps(client, ctx):
        core = ctx["core"]
        await sign_in(client)
        pages = {}
        for state in ("awaiting_confirmation", "searching", "failed"):
            j = core.add(
                job(
                    ctx["user"],
                    state,
                    error="The monthly budget for checks is used up."
                    if state == "failed"
                    else None,
                )
            )
            pages[state] = (await client.get(f"/checks/{j['id']}")).text
        history = (await client.get("/history")).text
        return pages, history

    pages, history = with_web(migrated_db, steps)
    assert "Is this the claim to check?" in pages["awaiting_confirmation"]
    assert 'sse-connect="/checks/' in pages["searching"]
    assert "budget for checks is used up" in pages["failed"]
    assert history.count('class="history__item"') == 3


def test_confirming_sends_the_edited_claim_to_core(migrated_db):
    async def steps(client, ctx):
        j = ctx["core"].add(job(ctx["user"], "awaiting_confirmation"))
        await sign_in(client)
        page = await client.get(f"/checks/{j['id']}")
        missing = await client.post(
            f"/checks/{j['id']}/confirm",
            data={"csrf_token": token(page.text), "supplement": "creatine"},
        )
        ok = await client.post(
            f"/checks/{j['id']}/confirm",
            data={
                "csrf_token": token(page.text),
                "supplement": "creatine",
                "effect": "stronger legs",
                "population": "",
            },
        )
        return missing, ok, ctx["core"].calls[-1]

    missing, ok, last = with_web(migrated_db, steps)
    assert (
        missing.status_code == 400
        and "Fill in the supplement and the effect" in missing.text
    )
    assert ok.status_code == 303
    assert last[2] == {
        "supplement": "creatine",
        "effect": "stronger legs",
        "population": None,
        "dose": None,
        "duration": None,
    }


def test_password_change_ends_other_sessions(migrated_db):
    async def steps(client, ctx):
        async with ctx["pool"].connection() as conn:
            elsewhere, _ = await store.create_session(conn, ctx["user"], T0, CFG)
        await sign_in(client)
        page = await client.get("/account")
        new = "an even longer new password"
        changed = await client.post(
            "/account/password",
            data={
                "csrf_token": token(page.text),
                "current_password": PASSWORD,
                "new_password": new,
                "confirm_password": new,
            },
        )
        after = (await client.get("/history")).status_code
        async with ctx["pool"].connection() as conn:
            other = await store.load_session(conn, elsewhere, T0, CFG)
        return changed, after, other

    changed, after, other = with_web(migrated_db, steps)
    assert changed.status_code == 200 and "Your password is changed" in changed.text
    assert after == 200, "this browser keeps a fresh session"
    assert other is None


@pytest.mark.parametrize("flag", [[], ["--admin"]])
def test_admin_cli_creates_accounts_and_refuses_duplicates(
    migrated_db, monkeypatch, flag
):
    conninfo = migrated_db.conninfo("web")
    email = f"new{len(flag)}@example.com"
    monkeypatch.setattr("sys.stdin", io.StringIO("a perfectly fine password\n"))
    message = run_async(
        admin.run(
            admin.parser().parse_args(["create-user", email, *flag]), conninfo, CFG
        )
    )
    assert message.startswith(f"Created {'admin' if flag else 'user'} account")

    monkeypatch.setattr("sys.stdin", io.StringIO("a perfectly fine password\n"))
    with pytest.raises(SystemExit, match="already exists"):
        run_async(
            admin.run(admin.parser().parse_args(["create-user", email]), conninfo, CFG)
        )

    run_async(admin.run(admin.parser().parse_args(["disable", email]), conninfo, CFG))

    async def check():
        pool = AsyncConnectionPool(conninfo, open=False)
        await pool.open()
        try:
            async with pool.connection() as conn:
                return await store.user_by_email(conn, email)
        finally:
            await pool.close()

    user = run_async(check())
    assert user.disabled and user.role == ("admin" if flag else "user")
