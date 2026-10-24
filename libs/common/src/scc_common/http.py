"""HTTP client for calls between modules: always a timeout, always the correlation ID (AGENTS.md section 7)."""

import httpx

from scc_common import correlation

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


async def _add_correlation_header(request: httpx.Request) -> None:
    correlation_id = correlation.current()
    if correlation_id:
        request.headers[correlation.HEADER] = correlation_id


def internal_client(
    base_url: str,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """A client for another module on the internal network. Close it, or use it with `async with`.

    `transport` is for tests only.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        event_hooks={"request": [_add_correlation_header]},
    )
