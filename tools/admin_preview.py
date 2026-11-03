"""Admin with sample data, for design work and browser checks without the dev stack (P3.08-P3.10).

Makes a throwaway database `scc_admin_preview` on the test PostgreSQL (SCC_TEST_ADMIN_DSN), runs
the canned claim end to end through the real pipeline and gateway with their recorded fixtures (no
live calls, nothing spent), adds sample jobs in every status, and serves web with core-api and the
gateway in one process at http://admin.localhost:18095/admin. Sign in as admin@example.com with the
preview-only password below. The database is dropped on a clean stop, and a hard stop's leftover at the next start.

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
from tests.admin_samples import seed, seed_calls

DBNAME = "scc_admin_preview"
PREFIX = "pv_"
APP_PASSWORD = "preview-only"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = (
    "admin preview only"  # a throwaway local database; never a real account
)
PORT = 18095
# The test container from "Working on it" in README.md.
LOCAL_TEST_DSN = "host=127.0.0.1 port=15433 user=postgres dbname=postgres"
FIXTURES = ROOT / "tests" / "fixtures" / "canned_claim"
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
            conn,
            "tester@example.com",
            passwords.hash_password(os.urandom(16).hex()),
            "user",
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
        async with pools["gateway"].connection() as conn:
            await seed_calls(conn, str(ids["failed_late"]), clear=False)
        beat = asyncio.create_task(heartbeat(pools["core"]))

        web = build_app(
            {},
            deps=Deps(
                AsyncConnectionPool(app_conninfo(conninfo, "web"), open=False),
                CoreClient("http://core", transport=httpx.ASGITransport(app=core_app)),
                web_config.load(),
                admin=AdminClient(
                    "http://core",
                    "http://gateway",
                    httpx.ASGITransport(app=core_app),
                    httpx.ASGITransport(app=gateway_app),
                ),
                admin_host="admin.localhost",
            ),
        )
        print(
            f"admin preview: http://admin.localhost:{PORT}/admin  (sign in as {ADMIN_EMAIL})"
        )
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
