"""Correlation IDs: one ID follows a request through every module and job (P1.01, AGENTS.md section 7).

The ID arrives in the `X-Correlation-ID` header, or is created at the first module that sees the request.
It lives in a context variable, so every log line written while handling the request carries it,
and `internal_client` (scc_common.http) sends it on to the next module. Jobs store it with their
payload and re-enter it with `bind()` when a worker picks them up.
"""

import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

HEADER = "X-Correlation-ID"
# Only plain IDs are accepted from outside, so a header can't inject text into the logs.
_VALID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_current: ContextVar[str | None] = ContextVar("correlation_id", default=None)

log = logging.getLogger("scc.request")


def new_id() -> str:
    return uuid.uuid4().hex


def is_valid(value: str | None) -> bool:
    return bool(value) and _VALID.fullmatch(value) is not None


def current() -> str | None:
    """The correlation ID of the request or job being handled, if any."""
    return _current.get()


@contextmanager
def bind(correlation_id: str | None) -> Iterator[str]:
    """Runs a block under a correlation ID, for example a job picked up by a worker.

    An invalid or missing ID is replaced by a new one, which is yielded.
    """
    value = correlation_id if is_valid(correlation_id) else new_id()
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)


class CorrelationMiddleware:
    """ASGI middleware: reads or creates the ID, returns it in the response, and logs one line per request."""

    # Liveness probes run every few seconds; logging them would bury everything else.
    QUIET_PATHS = frozenset({"/health"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name == b"x-correlation-id":
                incoming = value.decode("latin-1")
                break

        status = 500
        started = time.perf_counter()
        with bind(incoming) as correlation_id:

            async def send_with_header(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    headers = list(message.get("headers", []))
                    headers.append((b"x-correlation-id", correlation_id.encode()))
                    message = {**message, "headers": headers}
                await send(message)

            try:
                await self.app(scope, receive, send_with_header)
            finally:
                if scope["path"] not in self.QUIET_PATHS:
                    log.info(
                        "request",
                        extra={
                            "method": scope["method"],
                            "path": scope["path"],
                            "status": status,
                            "duration_ms": round(
                                (time.perf_counter() - started) * 1000, 1
                            ),
                        },
                    )
