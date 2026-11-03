"""P3.08-P3.10 acceptance: the admin pages, end to end in one process.

web, core-api and the gateway each run as an ASGI app on one migrated test database; web reaches the
other two through its AdminClient, as it does on the internal network. Only the queue is in memory.
"""

import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from procrastinate import App
from procrastinate.testing import InMemoryConnector
from psycopg_pool import AsyncConnectionPool

from scc_core import api as core_api
from scc_core import tasks
from scc_gateway import config as gateway_config
from scc_gateway import main as gateway_main
from scc_web import admin_views, config
from scc_web.admin_client import AdminClient
from scc_web.auth import passwords, store
from scc_web.context import Deps
from scc_web.core_client import CoreClient
from scc_web.main import build_app
from tests.admin_samples import seed, seed_calls
from tests.helpers import run_async
from tests.web_samples import FakeCore

CFG = config.load()
PASSWORD = "a long test password"
ADMIN_HOST = "admin.test"
ROOT = Path(__file__).resolve().parents[2]


class Down(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx.ConnectError("refused", request=request)


def with_admin(
    migrated_db,
    steps,
    *,
    trust_cloudflare=False,
    admin_host=ADMIN_HOST,
    gateway_down=False,
):
    """Runs `steps(ctx)` with web, core-api and the gateway wired together over ASGI."""

    async def scenario():
        pools = {
            m: AsyncConnectionPool(migrated_db.conninfo(m), open=False)
            for m in ("web", "core", "gateway")
        }
        for pool in pools.values():
            await pool.open()
        connector = InMemoryConnector()
        queue = App(connector=connector)
        queue.add_tasks_from(tasks.blueprint, namespace="core")
        core = core_api.build_app(checks={}, deps=core_api.Deps(pools["core"], queue))
        gateway = gateway_main.build_app(
            checks={},
            deps=gateway_main.Deps(pools["gateway"], None, None, gateway_config.load()),
        )
        deps = Deps(
            pools["web"],
            CoreClient("http://core", transport=FakeCore().transport()),
            CFG,
            trust_cloudflare=trust_cloudflare,
            admin=AdminClient(
                "http://core",
                "http://gateway",
                httpx.ASGITransport(app=core),
                Down() if gateway_down else httpx.ASGITransport(app=gateway),
            ),
            admin_host=admin_host,
        )
        web = build_app({}, deps=deps)
        try:
            async with pools["web"].connection() as conn:
                for table in ("sessions", "login_attempts", "users"):
                    await conn.execute(f"DELETE FROM auth.{table}")
                admin_id = await store.create_user(
                    conn,
                    "owner@example.com",
                    passwords.hash_password(PASSWORD),
                    "admin",
                )
                user_id = await store.create_user(
                    conn,
                    "tester@example.com",
                    passwords.hash_password(PASSWORD),
                    "user",
                )
            async with pools["core"].connection() as conn:
                ids = await seed(conn, user=user_id, other=admin_id)
            async with pools["gateway"].connection() as conn:
                await seed_calls(conn, str(ids["failed_late"]))
            ctx = {
                "ids": ids,
                "admin_id": admin_id,
                "user_id": user_id,
                "pools": pools,
                "queue": connector,
            }

            def client(host=ADMIN_HOST, **headers):
                return httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=web),
                    base_url=f"https://{host}",
                    headers=headers,
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


async def sign_in(client, email="owner@example.com"):
    csrf = token((await client.get("/sign-in")).text)
    response = await client.post(
        "/sign-in",
        data={
            "email": email,
            "password": PASSWORD,
            "csrf_token": csrf,
            "next": "/admin",
        },
    )
    assert response.status_code == 303, response.text
    return response


@asynccontextmanager
async def admin_client(ctx, **headers):
    async with ctx["client"](**headers) as client:
        await sign_in(client)
        yield client


def test_admin_answers_only_on_its_host_and_only_to_admins(migrated_db):
    async def steps(ctx):
        out = {}
        async with ctx["client"]("web.test") as public:
            await sign_in(public)
            out["public host, admin"] = (await public.get("/admin")).status_code
        async with ctx["client"]() as anonymous:
            response = await anonymous.get("/admin/jobs")
            out["anonymous"] = (response.status_code, response.headers.get("location"))
        async with ctx["client"]() as user:
            await sign_in(user, "tester@example.com")
            out["user"] = (await user.get("/admin")).status_code
        async with ctx["client"]("admin.test:8443") as owner:
            await sign_in(owner)
            response = await owner.get("/admin")
            out["admin"] = response.status_code
            out["headers"] = response.headers
        return out

    out = with_admin(migrated_db, steps)
    assert out["public host, admin"] == 404
    assert out["anonymous"] == (303, "/sign-in?next=/admin/jobs")
    assert out["user"] == 403
    assert out["admin"] == 200
    assert "frame-ancestors 'none'" in out["headers"]["content-security-policy"]
    assert out["headers"]["cache-control"] == "no-store"


def test_behind_the_tunnel_admin_needs_the_cloudflare_access_header(migrated_db):
    async def steps(ctx):
        async with ctx["client"]() as plain:
            await sign_in(plain)
            without = (await plain.get("/admin")).status_code
        async with ctx["client"](**{"cf-access-jwt-assertion": "eyJ.x.y"}) as through:
            await sign_in(through)
            with_header = (await through.get("/admin")).status_code
        return without, with_header

    assert with_admin(migrated_db, steps, trust_cloudflare=True) == (404, 200)


def test_without_an_admin_host_admin_does_not_exist(migrated_db):
    async def steps(ctx):
        async with ctx["client"]() as owner:
            await sign_in(owner)
            return (await owner.get("/admin")).status_code

    assert with_admin(migrated_db, steps, admin_host=None) == 404


def test_overview_and_jobs_by_status_search_and_email(migrated_db):
    async def steps(ctx):
        async with admin_client(ctx) as c:
            return (
                ctx["ids"],
                (await c.get("/admin")).text,
                (await c.get("/admin/jobs")).text,
                (await c.get("/admin/jobs", params={"status": "failed"})).text,
                (await c.get("/admin/jobs", params={"q": "owner@"})).text,
                (await c.get("/admin/jobs", params={"q": "nobody@"})).text,
                (
                    await c.get("/admin/jobs", params={"status": "bogus", "page": "1"})
                ).status_code,
            )

    ids, overview, everything, failed, by_email, nobody, bogus = with_admin(
        migrated_db, steps
    )
    assert "Today in SuppClaimCheck" in overview and "Latest errors" in overview
    assert everything.count('href="/admin/jobs/') == 5
    assert "tester@example.com" in everything
    assert 'Failed <span class="tabs__n num">2</span>' in everything
    assert str(ids["failed_late"]) in failed and str(ids["done"]) not in failed
    assert "provider_unavailable" in failed and admin_views.RB["RB-08"] in failed
    assert f'href="/admin/jobs/{ids["failed_late"]}"' in by_email
    assert by_email.count('href="/admin/jobs/') == 1
    assert "No job matches this search" in nobody
    assert bogus == 200  # an unknown status shows every job


def test_trace_shows_every_part_of_a_job_and_its_llm_calls(migrated_db):
    async def steps(ctx):
        async with admin_client(ctx) as c:
            return (
                ctx["ids"],
                (await c.get(f"/admin/jobs/{ctx['ids']['failed_late']}")).text,
                (await c.get(f"/admin/jobs/{ctx['ids']['done']}")).text,
                (await c.get(f"/admin/jobs/{ctx['ids']['stalled']}")).text,
                (await c.get("/admin/jobs/not-a-uuid")).status_code,
                (
                    await c.get("/admin/jobs/00000000-0000-0000-0000-000000000000")
                ).status_code,
            )

    ids, failed, done, stalled, bad, missing = with_admin(migrated_db, steps)
    for part in ("Timeline", "Input and claims", "LLM calls", "Prompts and settings"):
        assert part in failed
    assert "provider_unavailable" in failed and "Retry the search" in failed
    assert f"grep -E &#39;corr{ids['failed_late'].hex[:12]}|corr-1234&#39;" in failed
    assert "€0.0002" in failed and "2 calls" in failed  # from the gateway's records
    assert f"/admin/jobs/{ids['failed_late']}/passages/PMC6543210" in failed
    assert (
        "The <code>extract</code> prompt" in failed
    )  # from pipeline-v4 in core's store
    assert "Retry" not in done and "Upper-body strength increased" in done
    assert "not_in_passage" in done and "Made-up words." in done
    assert "This job looks stuck" in stalled
    assert (bad, missing) == (404, 404)


def test_retry_needs_the_form_token_then_queues_the_failed_phase(migrated_db):
    async def steps(ctx):
        job = ctx["ids"]["failed_late"]
        async with admin_client(ctx) as c:
            page = (await c.get(f"/admin/jobs/{job}")).text
            forged = await c.post(
                f"/admin/jobs/{job}/retry", data={"csrf_token": "forged"}
            )
            queued_before = len(ctx["queue"].jobs)
            done = await c.post(
                f"/admin/jobs/{job}/retry", data={"csrf_token": token(page)}
            )
            again = await c.post(
                f"/admin/jobs/{job}/retry", data={"csrf_token": token(page)}
            )
            after = (await c.get(done.headers["location"])).text
        return job, forged, queued_before, done, again, after, ctx

    job, forged, queued_before, done, again, after, ctx = with_admin(migrated_db, steps)
    assert (
        forged.headers["location"].endswith("?retried=expired") and queued_before == 0
    )
    assert done.headers["location"] == f"/admin/jobs/{job}?retried=searching"
    assert again.headers["location"].endswith("?retried=not-failed")
    assert "Retry queued" in after
    assert [j["task_name"] for j in ctx["queue"].jobs.values()] == [tasks.RUN]
    assert f"retry by admin user {ctx['admin_id']}" in after


def test_passages_mark_the_quoted_ones(migrated_db):
    async def steps(ctx):
        job = ctx["ids"]["failed_late"]
        async with admin_client(ctx) as c:
            return (
                (await c.get(f"/admin/jobs/{job}/passages/PMC6543210")).text,
                (await c.get(f"/admin/jobs/{job}/passages/PMC999")).status_code,
                (await c.get(f"/admin/jobs/{job}/passages/nonsense")).status_code,
            )

    found, other, bad = with_admin(migrated_db, steps)
    assert found.count('<li class="passage') == 3 and "Passage 2." in found
    assert (other, bad) == (404, 404)


def test_errors_costs_evals_and_health(migrated_db):
    async def steps(ctx):
        async with ctx["pools"]["core"].connection() as conn:
            await conn.execute(
                "INSERT INTO core.procrastinate_workers (last_heartbeat) VALUES (now())"
            )
            await conn.execute(
                "INSERT INTO core.config_evals (version, run_id, split, run_date, passed, cases,"
                " thresholds_met, attached_by) VALUES ('pipeline-v4', '2026-11-02-held-out', 'held_out',"
                " '2026-11-02', 5, 8, '{\"verdict_accuracy\": true, \"refusal\": false}', 'test')"
            )
        async with admin_client(ctx) as c:
            return {
                name: (await c.get(f"/admin/{name}")).text
                for name in ("errors", "costs", "evals", "health")
            }

    pages = with_admin(migrated_db, steps)
    errors = pages["errors"]
    for kind in ("Job failed", "Job stalled", "Quote dropped", "LLM call failed"):
        assert kind in errors
    assert admin_views.RB["RB-08"] in errors
    costs = pages["costs"]
    assert "€0.0002" in costs and "€15.00" in costs and "eval-case-03" in costs
    evals = pages["evals"]
    assert "2026-11-02-held-out" in evals and "5 of 8" in evals and "missed" in evals
    health = pages["health"]
    for module in ("web", "core-api", "core-worker", "gateway", "postgres", "edge"):
        assert f'data-label="Module">{module}</th>' in health
    assert "1 worker(s)" in health and "PostgreSQL 18" in health
    assert health.count("Down") == 0


def test_a_module_that_does_not_answer_is_named(migrated_db):
    async def steps(ctx):
        async with admin_client(ctx) as c:
            return (
                await c.get("/admin/costs"),
                (await c.get(f"/admin/jobs/{ctx['ids']['failed_late']}")).text,
                (await c.get("/admin/health")).text,
            )

    costs, trace, health = with_admin(migrated_db, steps, gateway_down=True)
    assert costs.status_code == 503 and "gateway didn't answer" in costs.text
    assert "The gateway did not answer" in trace  # the trace still shows core's part
    assert "unreachable" in health and admin_views.RB["RB-03"] in health


def test_config_versions_and_diffs_for_every_module(migrated_db):
    async def steps(ctx):
        async with admin_client(ctx) as c:
            return (
                (await c.get("/admin/config")).text,
                (await c.get("/admin/config/core/pipeline-v4")).text,
                (
                    await c.get(
                        "/admin/config/core/pipeline-v4",
                        params={"against": "pipeline-v0"},
                    )
                ).text,
                (await c.get("/admin/config/web/web-v0")).text,
                (await c.get("/admin/config/core/pipeline-v99")).status_code,
                (await c.get("/admin/config/nope/x")).status_code,
            )

    overview, v4, v4_from_v0, web, missing, bad = with_admin(migrated_db, steps)
    for label in ("Pipeline (core)", "Gateway", "Web"):
        assert label in overview
    assert 'href="/admin/config/gateway/gateway-v0"' in overview
    assert "What changes from <code>pipeline-v3</code>" in v4
    assert 'class="diff__add">+version = &#34;pipeline-v4&#34;' in v4
    assert "What changes from <code>pipeline-v0</code>" in v4_from_v0
    assert "web-v0" in web and "No lines differ" not in web
    assert (missing, bad) == (404, 404)


def test_users_create_reset_and_disable_from_admin(migrated_db):
    async def steps(ctx):
        async with admin_client(ctx) as c:
            page = (await c.get("/admin/users")).text
            csrf = token(page)
            created = await c.post(
                "/admin/users",
                data={"csrf_token": csrf, "email": "new@example.com", "role": "user"},
            )
            duplicate = await c.post(
                "/admin/users",
                data={"csrf_token": csrf, "email": "NEW@example.com", "role": "user"},
            )
            forged = await c.post(
                "/admin/users",
                data={"csrf_token": "x", "email": "x@example.com", "role": "user"},
            )
            async with ctx["client"]() as tester:
                await sign_in(tester, "tester@example.com")
                reset = await c.post(
                    f"/admin/users/{ctx['user_id']}/reset", data={"csrf_token": csrf}
                )
                tester_after = (await tester.get("/history")).status_code
            disabled = await c.post(
                f"/admin/users/{ctx['user_id']}/disable", data={"csrf_token": csrf}
            )
            self_disable = await c.post(
                f"/admin/users/{ctx['admin_id']}/disable", data={"csrf_token": csrf}
            )
            self_reset = await c.post(
                f"/admin/users/{ctx['admin_id']}/reset", data={"csrf_token": csrf}
            )
            missing = await c.post(
                "/admin/users/999999/disable", data={"csrf_token": csrf}
            )
        async with ctx["pools"]["web"].connection() as conn:
            new = await store.user_by_email(conn, "new@example.com")
            tester = await store.user_by_email(conn, "tester@example.com")
        return (
            created,
            duplicate,
            forged,
            reset,
            tester_after,
            disabled,
            self_disable,
            self_reset,
            missing,
            new,
            tester,
        )

    (created, duplicate, forged, reset, tester_after, disabled, self_disable, self_reset,
     missing, new, tester) = with_admin(migrated_db, steps)  # fmt: skip
    shown = re.search(r'<p class="secret"><code>([^<]+)</code>', created.text).group(1)
    assert created.status_code == 200 and "Account created" in created.text
    assert (
        new is not None
        and new.role == "user"
        and passwords.verify(new.password_hash, shown)
    )
    assert duplicate.status_code == 400 and "already exists" in duplicate.text
    assert forged.status_code == 400 and "This form expired" in forged.text
    reset_to = re.search(r'<p class="secret"><code>([^<]+)</code>', reset.text).group(1)
    assert "Password reset" in reset.text and reset_to != shown
    assert tester_after == 303  # the reset ended the tester's session
    assert "is disabled and signed out everywhere" in disabled.text and tester.disabled
    assert self_disable.status_code == 400 and "can&#39;t disable" in self_disable.text
    assert self_reset.status_code == 400 and "under Account" in self_reset.text
    assert missing.status_code == 404


@pytest.mark.parametrize("entry", sorted(admin_views.RB))
def test_every_runbook_link_points_at_a_heading(entry):
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to hyphens."""
    headings = re.findall(
        r"^## (RB-\d\d .+)$",
        (ROOT / "docs" / "RUNBOOK.md").read_text("utf-8"),
        re.MULTILINE,
    )
    anchors = {
        re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in headings
    }
    assert admin_views.RB[entry] in anchors
    assert all(rb in admin_views.RB for _, rb in admin_views.CODES.values() if rb)
