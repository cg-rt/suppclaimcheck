"""Starts a module's HTTP server with JSON logging (the container command is `python -m scc_<module>`)."""

import uvicorn

from scc_common.logs import configure_logging


def serve(factory: str, service: str, port: int = 8000) -> None:
    configure_logging(service)
    uvicorn.run(
        factory,
        factory=True,
        host="0.0.0.0",  # inside the container; compose decides what is reachable
        port=port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
