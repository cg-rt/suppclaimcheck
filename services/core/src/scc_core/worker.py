"""core-worker, placeholder until P1.05 brings the Procrastinate worker.

It starts, logs, and touches a heartbeat file that the container health check reads.
Jobs will carry their correlation ID in the payload and run under `scc_common.correlation.bind()`.
"""

import logging
import signal
import threading
from pathlib import Path

from scc_common.logs import configure_logging

HEARTBEAT = Path("/tmp/heartbeat")
INTERVAL_S = 10

log = logging.getLogger("scc.worker")


def main() -> None:
    configure_logging("core-worker")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("worker started")
    while not stop.is_set():
        HEARTBEAT.touch()
        stop.wait(INTERVAL_S)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
