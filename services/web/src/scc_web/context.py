"""Shared state for web's routes: dependencies, the signed-in user, and template rendering."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from psycopg_pool import AsyncConnectionPool

from scc_web.admin_client import AdminClient
from scc_web.auth import store
from scc_web.core_client import CoreClient
from scc_web.security import SESSION_COOKIE

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
STATIC = Path(__file__).parent / "static"


@cache
def asset(path: str) -> str:
    """A static file's URL with a hash of its content. A deploy that changes the CSS changes the URL, so
    no browser or Cloudflare cache serves the old file with the new pages."""
    digest = hashlib.sha256((STATIC / path).read_bytes()).hexdigest()[:12]
    return f"/static/{path}?v={digest}"


templates.env.globals["asset"] = asset


@dataclass
class Deps:
    pool: AsyncConnectionPool
    core: CoreClient
    config: dict
    trust_cloudflare: bool = False
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Admin (P3.08-P3.10): its hostname and the client for core's and the gateway's admin endpoints.
    admin: AdminClient | None = None
    admin_host: str | None = None
    # The person or organisation named as the controller on the privacy page (P3.06).
    operator: str | None = None


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
    """Only same-site paths, so the sign-in form can't be used to redirect elsewhere. Spaces and control
    characters are refused too: browsers drop tabs and line breaks from a URL, so "/\\t/evil.example"
    would become "//evil.example" if anything passed it on unencoded (P3.07)."""
    if (
        path
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and all(" " < ch < "\x7f" for ch in path)
    ):
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
