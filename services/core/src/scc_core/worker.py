"""core-worker: runs the pipeline tasks from the Procrastinate queue (P1.05, P1.08).

It also touches a heartbeat file every few seconds; the container health check reads it, so a
worker whose event loop is stuck turns unhealthy. No internet: every outside call goes through the gateway.
"""

import asyncio
import logging
import os
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

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
    pool = AsyncConnectionPool(conninfo, open=False, min_size=1, max_size=4)
    await pool.open()
    tasks.runtime = Runtime(
        pool=pool,
        gateway=GatewayClient(os.environ["SCC_GATEWAY_URL"]),
        config=config.load(),
    )
    app = tasks.make_app(conninfo)
    beat = asyncio.create_task(_heartbeat())
    log.info(
        "worker started", extra={"config_version": tasks.runtime.config["version"]}
    )
    try:
        async with app.open_async():
            await app.run_worker_async(
                queues=[tasks.QUEUE], concurrency=2, install_signal_handlers=True
            )
    finally:
        beat.cancel()
        await pool.close()
        log.info("worker stopped")


def main() -> None:
    configure_logging("core-worker")
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
