"""core-worker: runs the pipeline tasks from the Procrastinate queue (P1.05, P1.08).

It also touches a heartbeat file every few seconds; the container health check reads it, so a
worker whose event loop is stuck turns unhealthy. No internet: every outside call goes through the gateway.
"""

import asyncio
import logging
import os
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from scc_common import config_store
from scc_common.db import conninfo_from_env
from scc_common.logs import configure_logging
from scc_core import config, tasks
from scc_core.gateway_client import GatewayClient
from scc_core.pipeline.run import Runtime

HEARTBEAT = Path("/tmp/heartbeat")
INTERVAL_S = 10

log = logging.getLogger("scc.worker")


async def _heartbeat() -> None:
    while True:
        HEARTBEAT.touch()
        await asyncio.sleep(INTERVAL_S)


async def main_async() -> None:
    conninfo = conninfo_from_env()
    # Read the active config before opening anything that runs in the background. With the database
    # down this raises and the process exits, so Docker restarts it until the database answers
    # (RB-04). Opened first, the pool's reconnect task held the exit open: the worker hung, unhealthy
    # and silent, and no check ran until someone restarted it (found in the P3.13 rehearsal).
    active = config.load_active(conninfo)
    pool = AsyncConnectionPool(conninfo, open=False, min_size=1, max_size=4)
    await pool.open()
    beat = None
    try:

        async def load_version(version: str) -> dict:
            async with pool.connection() as conn:
                return await config_store.load_version_async(
                    conn, config.STORE, version
                )

        tasks.runtime = Runtime(
            pool=pool,
            gateway=GatewayClient(os.environ["SCC_GATEWAY_URL"]),
            config=active,
            load_config=load_version,
        )
        app = tasks.make_app(conninfo)
        beat = asyncio.create_task(_heartbeat())
        log.info("worker started", extra={"config_version": active["version"]})
        async with app.open_async():
            await app.run_worker_async(
                queues=[tasks.QUEUE], concurrency=2, install_signal_handlers=True
            )
    finally:
        if beat is not None:
            beat.cancel()
        await pool.close()
        log.info("worker stopped")


def main() -> None:
    configure_logging("core-worker")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
