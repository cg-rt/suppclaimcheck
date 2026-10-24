"""P1.14 acceptance: a failed job alerts the owner through the gateway, once, without the user's text.

Real core pipeline and gateway app against a real database; the gateway has no LLM key, so extraction
fails with provider_not_configured. Telegram is a mock transport.
"""

import json

import httpx
from psycopg_pool import AsyncConnectionPool

from scc_core import config as core_config
from scc_core import jobs
from scc_core.gateway_client import GatewayClient
from scc_core.models import JobCreate
from scc_core.pipeline import run
from scc_gateway import config as gateway_config
from scc_gateway import main as gateway_main
from scc_gateway.alerts import Alerts
from scc_gateway.literature import Literature
from scc_gateway.llm import MissingProvider
from tests.helpers import run_async

SECRET_CLAIM = "My private note: magnesium fixes my sleep."


def test_failed_jobs_send_one_alert_with_the_code_not_the_text(migrated_db):
    sent: list[dict] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    async def scenario():
        gateway_pool = AsyncConnectionPool(migrated_db.conninfo("gateway"), open=False)
        core_pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await gateway_pool.open()
        await core_pool.open()
        try:
            gcfg = gateway_config.load()
            alerts = Alerts(
                gcfg, "token", "42", transport=httpx.MockTransport(telegram)
            )
            gateway_app = gateway_main.build_app(
                checks={},
                deps=gateway_main.Deps(
                    gateway_pool,
                    MissingProvider(),
                    Literature(gcfg, None, gateway_pool),
                    gcfg,
                    alerts,
                ),
            )
            gateway = GatewayClient(
                "http://gateway", transport=httpx.ASGITransport(app=gateway_app)
            )
            rt = run.Runtime(core_pool, gateway, core_config.load())
            states = []
            for i in range(2):
                async with core_pool.connection() as conn:
                    job, _ = await jobs.create(
                        conn,
                        JobCreate(user_id=1, input_text=SECRET_CLAIM),
                        f"alert-key-{i}",
                        "pipeline-v0",
                        None,
                    )
                await run.extract(rt, job.id)
                async with core_pool.connection() as conn:
                    states.append((await jobs.get(conn, job.id)).state)
            return states
        finally:
            await core_pool.close()
            await gateway_pool.close()

    states = run_async(scenario())
    assert states == ["failed", "failed"]
    assert len(sent) == 1, "the second failure with the same code is held back"
    text = sent[0]["text"]
    assert "job_failed · provider_not_configured" in text
    assert "magnesium" not in text
