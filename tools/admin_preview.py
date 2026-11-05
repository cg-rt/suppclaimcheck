"""Admin with sample data, for design work and browser checks without the dev stack (P3.08-P3.10).

Makes a throwaway database `scc_admin_preview` on the test PostgreSQL (SCC_TEST_ADMIN_DSN), runs
the canned claim end to end through the real pipeline and gateway with their recorded fixtures (no
live calls, nothing spent), adds sample jobs in every status, and serves web with core-api and the
gateway in one process at http://admin.localhost:18095/admin. Sign in as admin@example.com with the
preview-only password below. The public pages answer at http://localhost:18095 (the tester account,
with its own preview-only password, owns the canned check, with feedback and times for P4.03 and
P4.04, and three link checks for P3.12: one waiting for a pick, one finished, one the site kept out). The database is dropped on a clean stop, and a hard stop's leftover at the next start.

Run from the repo root (`.claude/launch.json` has it as `admin-preview`):
    uv run python tools/admin_preview.py
SCC_TEST_ADMIN_DSN picks another server than the local test container.
"""

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

import anthropic
import httpx
import httpx2
import psycopg
import uvicorn
from procrastinate import App
from procrastinate.testing import InMemoryConnector
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # for tests.admin_samples

from scc_common.migrations import migrate
from scc_common.recorded import ReplayTransport
from scc_core import api as core_api
from scc_core import config as core_config
from scc_core import migrate as core_migrate
from scc_core import tasks
from scc_core.gateway_client import GatewayClient
from scc_core.pipeline.run import Runtime
from scc_gateway import config as gateway_config
from scc_gateway import main as gateway_main
from scc_gateway import migrate as gateway_migrate
from scc_gateway.literature import Literature
from scc_gateway.llm import ClaudeProvider
from scc_web import config as web_config
from scc_web import migrate as web_migrate
from scc_web.admin_client import AdminClient
from scc_web.auth import passwords, store
from scc_web.context import Deps
from scc_web.core_client import CoreClient
from scc_web.main import build_app
from tests.admin_samples import seed, seed_calls, seed_safety

DBNAME = "scc_admin_preview"
PREFIX = "pv_"
APP_PASSWORD = "preview-only"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = (
    "admin preview only"  # a throwaway local database; never a real account
)
TESTER_EMAIL = "tester@example.com"
TESTER_PASSWORD = "tester preview only"  # the same: a throwaway local database
PORT = 18095
# The test container from "Working on it" in README.md.
LOCAL_TEST_DSN = "host=127.0.0.1 port=15433 user=postgres dbname=postgres"
FIXTURES = ROOT / "tests" / "fixtures" / "canned_claim"
# P3.12: sample link checks, as the reader and the page prompt left them for a real page (dev drill).
LINK = "https://www.nccih.nih.gov/health/melatonin-what-you-need-to-know"
LINK_SOURCE = {
    "url": LINK,
    "final_url": LINK,
    "title": "Melatonin: What You Need To Know",
    "site_name": "NCCIH",
    "chars": 28027,
    "chars_read": 28027,
    "truncated": False,
}
LINK_CLAIMS = [
    ("Melatonin", "may help with jet lag symptoms after eastward flights",
     "travelers crossing multiple time zones",
     "melatonin may be better than a placebo (an inactive substance) in reducing jet lag symptoms"),
    ("Melatonin", "reduces anxiety before surgery", "surgical patients",
     "melatonin is better than placebo at reducing anxiety before surgery"),
    ("Melatonin", "reduces time to fall asleep in delayed sleep-wake phase disorder",
     "people with delayed sleep-wake phase disorder",
     "it took about 22 minutes less for them to fall asleep"),
]  # fmt: skip


async def seed_links(conn, user: int) -> None:
    claims = [
        {"supplement": s, "effect": e, "population": p, "dose": None, "duration": None,
         "quote": q, "specific": True, "spelling_suggestion": None}
        for s, e, p, q in LINK_CLAIMS
    ]  # fmt: skip
    done = await conn.execute(
        "SELECT confirmed_claim, result FROM core.jobs WHERE state = 'done' AND user_id = %s"
        " AND input_type = 'text' ORDER BY created_at LIMIT 1",
        (user,),
    )
    confirmed, result = await done.fetchone()
    rows = [
        ("awaiting_confirmation", LINK_SOURCE, claims, None, None, None),
        ("done", LINK_SOURCE, claims, confirmed, result, None),
        ("failed", None, None, None, None,
         ("That site asks automated readers not to read this page, so SuppClaimCheck didn't. "
          "Paste the text with the claim instead.")),
    ]  # fmt: skip
    for n, (state, source, extracted, claim, outcome, error) in enumerate(rows):
        await conn.execute(
            "INSERT INTO core.jobs (user_id, idempotency_key, request_hash, state, input_type,"
            " input_text, source, source_text, extracted_claims, confirmed_claim, result, error,"
            " config_version, created_at) VALUES (%s, %s, 'preview', %s, 'url', %s, %s, %s, %s, %s,"
            " %s, %s, 'pipeline-v9', now() - make_interval(mins => %s))",
            (
                user, f"preview-link-{n}", state,
                LINK if source else "https://www.example.org/members/fish-oil",
                Jsonb(source) if source else None,
                "Melatonin: What You Need To Know\n\nMelatonin is a hormone..." if source else None,
                Jsonb(extracted) if extracted else None,
                Jsonb(claim) if claim else None,
                Jsonb(outcome) if outcome else None,
                error, 3 - n,
            ),
        )  # fmt: skip


MODULES = [web_migrate, core_migrate, gateway_migrate]


def specs():
    return {m.SPEC.module: dataclasses.replace(m.SPEC, prefix=PREFIX) for m in MODULES}


def drop(admin_dsn: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(DBNAME)
            )
        )
        for spec in specs().values():
            for role in (spec.app_role, spec.owner_role):
                admin.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )


def create(admin_dsn: str) -> str:
    drop(admin_dsn)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DBNAME)))
    conninfo = psycopg.conninfo.make_conninfo(admin_dsn, dbname=DBNAME)
    for m in MODULES:
        migrate(specs()[m.SPEC.module], m.MIGRATIONS, conninfo, APP_PASSWORD, m.STORE)
    return conninfo


def app_conninfo(admin_conninfo: str, module: str) -> str:
    return psycopg.conninfo.make_conninfo(
        admin_conninfo, user=specs()[module].app_role, password=APP_PASSWORD
    )


def fixture_llm():
    """The real Anthropic SDK, answering from the canned claim's recorded replies."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        schema = json.loads(request.content)["output_config"]["format"]["schema"]
        stage = "extract" if "claims" in schema["properties"] else "assess"
        return httpx2.Response(
            200,
            json=json.loads((FIXTURES / "llm" / f"{stage}.json").read_text("utf-8")),
        )

    return anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))


async def canned_claim(
    core: httpx.AsyncClient, connector: InMemoryConnector, user_id: int
):
    """One finished check through the real pipeline: extraction, confirmation, search, scoring."""
    created = await core.post(
        "/jobs",
        json={"user_id": user_id, "input_text": "Taking creatine while doing resistance "
              "training makes older adults stronger. Ask your coach about it!"},
        headers={"Idempotency-Key": "preview-canned-1", "X-Correlation-ID": "preview-canned-0001"},
    )  # fmt: skip
    job = created.json()
    queued = next(j for j in connector.jobs.values() if j["task_name"] == tasks.EXTRACT)
    await tasks.extract_claim(**queued["args"])
    extracted = (await core.get(f"/jobs/{job['id']}")).json()["extracted_claims"][0]
    claim = {
        k: extracted[k]
        for k in ("supplement", "effect", "population", "dose", "duration")
    }
    await core.post(f"/jobs/{job['id']}/confirm", json=claim)
    run = next(j for j in connector.jobs.values() if j["task_name"] == tasks.RUN)
    await tasks.run_pipeline(**run["args"])
    connector.jobs.clear()


async def heartbeat(pool: AsyncConnectionPool) -> None:
    """Plays a live core-worker for the health page."""
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "INSERT INTO core.procrastinate_workers DEFAULT VALUES RETURNING id"
            )
        ).fetchone()
    while True:
        await asyncio.sleep(10)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE core.procrastinate_workers SET last_heartbeat = now() WHERE id = %s",
                row,
            )


async def main(admin_dsn: str) -> None:
    conninfo = create(admin_dsn)
    pools = {
        m: AsyncConnectionPool(app_conninfo(conninfo, m), open=False)
        for m in ("core", "gateway")
    }
    for pool in pools.values():
        await pool.open()
    gcfg = gateway_config.load()
    gateway_app = gateway_main.build_app(
        checks={},
        deps=gateway_main.Deps(
            pools["gateway"],
            ClaudeProvider("fixture-only", 30, http_client=fixture_llm()),
            Literature(
                gcfg, None, pools["gateway"], ReplayTransport(FIXTURES / "literature")
            ),
            gcfg,
        ),
    )
    v0 = core_config.load(
        "pipeline-v0"
    )  # the canned claim's fixtures hold one article, as v0 reads
    tasks.runtime = Runtime(
        pools["core"],
        GatewayClient("http://gateway", transport=httpx.ASGITransport(app=gateway_app)),
        v0,
    )
    connector = InMemoryConnector()
    queue = App(connector=connector)
    queue.add_tasks_from(tasks.blueprint, namespace="core")
    core_app = core_api.build_app(
        checks={}, deps=core_api.Deps(pools["core"], queue, "pipeline-v0")
    )

    web_pool = AsyncConnectionPool(app_conninfo(conninfo, "web"), open=False)
    await web_pool.open()
    async with web_pool.connection() as conn:
        admin_id = await store.create_user(
            conn, ADMIN_EMAIL, passwords.hash_password(ADMIN_PASSWORD), "admin"
        )
        tester = await store.create_user(
            conn, TESTER_EMAIL, passwords.hash_password(TESTER_PASSWORD), "user"
        )
        gone = await store.create_user(
            conn,
            "former@example.com",
            passwords.hash_password(os.urandom(16).hex()),
            "user",
        )
        await store.disable_user(conn, gone)
    await web_pool.close()

    async with queue.open_async():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=core_app), base_url="http://core"
        ) as core:
            await canned_claim(core, connector, tester)
        async with pools["core"].connection() as conn:
            ids = await seed(conn, user=tester, other=admin_id, clear=False)
            await seed_safety(conn, user=tester, other=gone, reviewer=admin_id)
            await seed_links(conn, tester)
            # P4.04: the seeded rows are written directly, so they get times the trigger would give.
            await conn.execute(
                "UPDATE core.jobs SET confirmed_at = created_at + interval '25 seconds',"
                " finished_at = created_at + interval '71 seconds'"
                " WHERE state = 'done' AND finished_at IS NULL"
            )
            # P4.03: two ratings, one with a comment.
            done = await conn.execute(
                "SELECT id, user_id FROM core.jobs WHERE state = 'done' ORDER BY created_at LIMIT 2"
            )
            for (job_id, owner), answers in zip(
                await done.fetchall(),
                [("yes", "yes", None),
                 ("no", "partly", "The 2023 meta-analysis on creatine in women over 60.")],
                strict=False,
            ):  # fmt: skip
                await conn.execute(
                    "INSERT INTO core.feedback (job_id, user_id, verdict_right, understandable,"
                    " missing) VALUES (%s, %s, %s, %s, %s)",
                    (job_id, owner, *answers),
                )
        async with pools["gateway"].connection() as conn:
            await seed_calls(conn, str(ids["failed_late"]), clear=False)
            # P3.04: this month's live spend past 80% of the cap, so the banner and the
            # per-account costs have something to show.
            await conn.execute(
                "INSERT INTO gateway.llm_calls (lane, status, stage, job_id, user_id, model,"
                " input_tokens, output_tokens, cost_eur, latency_ms) VALUES ('live', 'ok', 'extract',"
                " 'preview-heavy', %s, 'claude-haiku-5-5', 900000, 40000, 12.4, 2100)",
                (tester,),
            )
        beat = asyncio.create_task(heartbeat(pools["core"]))

        web = build_app(
            {},
            deps=Deps(
                AsyncConnectionPool(app_conninfo(conninfo, "web"), open=False),
                # The active config (pipeline-v9 and later), so the new-check page offers links.
                CoreClient(
                    "http://core",
                    transport=httpx.ASGITransport(
                        app=core_api.build_app(
                            checks={}, deps=core_api.Deps(pools["core"], queue)
                        )
                    ),
                ),
                web_config.load(),
                admin=AdminClient(
                    "http://core",
                    "http://gateway",
                    # Admin reads the active config (pipeline-v5, with the injection throttle);
                    # only the canned claim above runs on v0, for its one-article fixtures.
                    httpx.ASGITransport(
                        app=core_api.build_app(
                            checks={}, deps=core_api.Deps(pools["core"], queue)
                        )
                    ),
                    httpx.ASGITransport(app=gateway_app),
                ),
                admin_host="admin.localhost",
            ),
        )
        print(
            f"admin preview: http://admin.localhost:{PORT}/admin  (sign in as {ADMIN_EMAIL})"
        )
        print(f"public pages: http://localhost:{PORT}  (sign in as {TESTER_EMAIL})")
        server = uvicorn.Server(
            uvicorn.Config(web, host="127.0.0.1", port=PORT, log_level="warning")
        )
        try:
            await server.serve()
        finally:
            beat.cancel()
            for pool in pools.values():
                await pool.close()


if __name__ == "__main__":
    dsn = os.environ.get("SCC_TEST_ADMIN_DSN", LOCAL_TEST_DSN)
    try:
        if (
            sys.platform == "win32"
        ):  # psycopg's async mode needs a selector loop on Windows
            asyncio.run(main(dsn), loop_factory=asyncio.SelectorEventLoop)
        else:
            asyncio.run(main(dsn))
    finally:
        drop(dsn)
