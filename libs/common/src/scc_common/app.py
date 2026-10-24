"""The FastAPI app every HTTP module starts from: JSON logs, correlation IDs, `/health`, `/health/deps` and `/version`."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from scc_common.correlation import CorrelationMiddleware
from scc_common.http import internal_client
from scc_common.version import version_info

# A dependency check returns normally when the dependency is usable and raises when it isn't.
Check = Callable[[], Awaitable[None]]
CHECK_TIMEOUT_S = 5.0

log = logging.getLogger("scc.health")


async def run_checks(checks: Mapping[str, Check]) -> tuple[bool, dict[str, str]]:
    async def one(name: str, check: Check) -> tuple[str, str]:
        try:
            await asyncio.wait_for(check(), CHECK_TIMEOUT_S)
            return name, "ok"
        except Exception as exc:  # noqa: BLE001 - any failure makes the dependency unhealthy
            log.warning(
                "dependency check failed", extra={"check": name, "error": repr(exc)}
            )
            return name, "failed"

    results = dict(await asyncio.gather(*(one(n, c) for n, c in checks.items())))
    return all(v == "ok" for v in results.values()), results


def create_app(
    service: str, title: str, checks: Mapping[str, Check] | None = None
) -> FastAPI:
    """`/health` says the process is up; `/health/deps` also checks the database and the next module."""
    app = FastAPI(title=title, version=version_info(service)["version"])
    app.add_middleware(CorrelationMiddleware)
    dependency_checks = dict(checks or {})

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/health/deps",
        tags=["ops"],
        responses={503: {"description": "A dependency failed"}},
    )
    async def health_deps() -> JSONResponse:
        ok, results = await run_checks(dependency_checks)
        return JSONResponse(
            {"status": "ok" if ok else "failed", "checks": results},
            status_code=200 if ok else 503,
        )

    @app.get("/version", tags=["ops"])
    async def version() -> dict[str, str]:
        return version_info(service)

    return app


def http_check(base_url: str, path: str = "/health/deps") -> Check:
    """A check that another module answers 200, sending the correlation ID along."""

    async def check() -> None:
        async with internal_client(base_url) as client:
            response = await client.get(path)
            response.raise_for_status()

    return check
