"""Shared state for web's routes: dependencies, the signed-in user, and template rendering."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from psycopg_pool import AsyncConnectionPool

from scc_web.auth import store
from scc_web.core_client import CoreClient
from scc_web.security import SESSION_COOKIE

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@dataclass
class Deps:
    pool: AsyncConnectionPool
    core: CoreClient
    config: dict
    trust_cloudflare: bool = False
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


class SignInRequired(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


def deps(request: Request) -> Deps:
    return request.app.state.deps


async def current_session(request: Request) -> store.Session | None:
    """The signed-in session for this request, looked up once and kept on the request."""
    if hasattr(request.state, "session"):
        return request.state.session
    session = None
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        d = deps(request)
        async with d.pool.connection() as conn:
            session = await store.load_session(conn, session_id, d.clock(), d.config)
    request.state.session = session
    return session


async def require_session(request: Request) -> store.Session:
    session = await current_session(request)
    if session is None:
        raise SignInRequired(request.url.path)
    return session


def safe_next(path: str | None) -> str:
    """Only same-site paths, so the sign-in form can't be used to redirect elsewhere."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/checks/new"


def sign_in_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(f"/sign-in?next={quote(next_path)}", status_code=303)


def render(request: Request, name: str, status_code: int = 200, **context) -> Response:
    session = getattr(request.state, "session", None)
    return templates.TemplateResponse(
        request,
        name,
        {
            "session": session,
            "csrf_token": session.csrf_token if session else None,
            **context,
        },
        status_code=status_code,
    )
