"""The web module: public site, login and (from P3) admin, all server-rendered (P1.04, P1.10)."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException as StarletteHTTPException

from scc_common.app import Check, create_app, http_check
from scc_common.db import conninfo_from_env, db_check
from scc_web import config, pages
from scc_web.auth import routes as auth_routes
from scc_web.context import Deps, SignInRequired, render, sign_in_redirect
from scc_web.core_client import CoreClient
from scc_web.security import SecurityHeadersMiddleware

log = logging.getLogger("scc.web")
STATIC = Path(__file__).parent / "static"


def build_app(
    checks: dict[str, Check] | None = None, deps: Deps | None = None
) -> FastAPI:
    if checks is None:
        checks = {
            "database": db_check(conninfo_from_env()),
            "core-api": http_check(os.environ["SCC_CORE_URL"]),
        }
    if deps is None and "SCC_DB_USER" in os.environ:
        deps = Deps(
            pool=AsyncConnectionPool(
                conninfo_from_env(), open=False, min_size=1, max_size=5
            ),
            core=CoreClient(os.environ["SCC_CORE_URL"]),
            config=config.load(),
            trust_cloudflare=os.environ.get("SCC_TRUST_CLOUDFLARE") == "1",
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if deps is not None:
            await deps.pool.open()
        try:
            yield
        finally:
            if deps is not None:
                await deps.pool.close()

    app = create_app("web", "SuppClaimCheck web", checks, lifespan=lifespan)
    app.state.deps = deps
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(auth_routes.router)
    app.include_router(pages.router)

    @app.exception_handler(SignInRequired)
    async def to_sign_in(request: Request, exc: SignInRequired) -> Response:
        return sign_in_redirect(exc.next_path)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return render(
                request,
                "error.html",
                404,
                title="Page not found",
                message="There's nothing at this address, or it belongs to someone else.",
            )
        return render(
            request,
            "error.html",
            exc.status_code,
            title="Something went wrong",
            message="Please try again.",
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        # Users never see a stack trace; the log has it, under the request's correlation ID.
        log.exception("unhandled error")
        return render(
            request,
            "error.html",
            500,
            title="Something went wrong",
            message="The page could not be shown. Please try again in a minute.",
        )

    return app
