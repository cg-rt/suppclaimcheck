"""Security plumbing for web (P1.04, D-016): headers, cookies, CSRF and the client's address.

- Session cookie `__Host-scc_session`: HttpOnly, Secure, SameSite=Lax, Path=/, no Domain. The
  `__Host-` prefix makes the browser refuse it unless all of that holds.
- CSRF: every form carries a token. Signed-in forms use the session's token (synchronizer); the
  sign-in form, which has no session yet, uses a double-submit cookie `__Host-scc_csrf`.
- Strict content security policy: scripts, styles and fonts only from this site, no inline code.
"""

import secrets

from fastapi import Request
from fastapi.responses import Response

SESSION_COOKIE = "__Host-scc_session"
PRE_CSRF_COOKIE = "__Host-scc_csrf"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class SecurityHeadersMiddleware:
    """Adds the security headers to every response, and no-store to every page that isn't a static file."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_static = scope["path"].startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in HEADERS.items():
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))
                if not is_static and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def set_session_cookie(response: Response, session_id: str, max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )


def new_pre_csrf(response: Response) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        PRE_CSRF_COOKIE, token, path="/", secure=True, httponly=True, samesite="strict"
    )
    return token


def tokens_match(expected: str | None, given: str | None) -> bool:
    return bool(expected) and bool(given) and secrets.compare_digest(expected, given)


def client_address(request: Request, trust_cloudflare: bool) -> str:
    """The visitor's address. Behind the tunnel every request comes from edge, so Cloudflare's header is used."""
    if trust_cloudflare:
        forwarded = request.headers.get("cf-connecting-ip")
        if forwarded:
            return forwarded.strip()
    return request.client.host if request.client else "unknown"
