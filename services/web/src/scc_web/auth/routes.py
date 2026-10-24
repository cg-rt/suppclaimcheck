"""Sign in, sign out and change password (P1.04). Accounts are created by the admin CLI only: no sign-up."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from scc_web.auth import passwords, store
from scc_web.context import current_session, deps, render, require_session, safe_next
from scc_web.security import (
    PRE_CSRF_COOKIE,
    clear_session_cookie,
    client_address,
    new_pre_csrf,
    set_session_cookie,
    tokens_match,
)

log = logging.getLogger("scc.auth")
router = APIRouter()

WRONG = "That email and password don't match an account. Check both and try again."
EXPIRED = "This form expired. It has been reloaded: try again."


@router.get("/sign-in")
async def sign_in_form(request: Request) -> Response:
    if await current_session(request):
        return RedirectResponse(
            safe_next(request.query_params.get("next")), status_code=303
        )
    token_holder = Response()
    token = new_pre_csrf(token_holder)
    response = render(
        request,
        "sign_in.html",
        next=safe_next(request.query_params.get("next")),
        pre_csrf=token,
    )
    response.headers.append("set-cookie", token_holder.headers["set-cookie"])
    return response


@router.post("/sign-in")
async def sign_in(request: Request) -> Response:
    d = deps(request)
    form = await request.form()
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))
    next_path = safe_next(str(form.get("next", "")))

    def again(message: str, status_code: int) -> Response:
        holder = Response()
        token = new_pre_csrf(holder)
        response = render(
            request,
            "sign_in.html",
            status_code,
            next=next_path,
            pre_csrf=token,
            email=email,
            error=message,
        )
        response.headers.append("set-cookie", holder.headers["set-cookie"])
        return response

    if not tokens_match(
        request.cookies.get(PRE_CSRF_COOKIE), str(form.get("csrf_token", ""))
    ):
        log.warning("sign-in rejected: csrf")
        return again(EXPIRED, 400)

    now = d.clock()
    address = client_address(request, d.trust_cloudflare)
    async with d.pool.connection() as conn:
        if await store.sign_in_blocked(conn, email, address, now, d.config):
            log.warning("sign-in blocked", extra={"address": address})
            minutes = d.config["login"]["window_minutes"]
            return again(
                f"Too many sign-in attempts. Wait {minutes} minutes, then try again.",
                429,
            )
        user = await store.user_by_email(conn, email)
        valid = (
            passwords.verify(user.password_hash if user else None, password)
            and user is not None
            and not user.disabled
        )
        await store.record_attempt(conn, email, address, valid, now)
        if not valid:
            log.info("sign-in failed")
            return again(WRONG, 400)
        if passwords.needs_rehash(user.password_hash):
            await store.set_password(conn, user.id, passwords.hash_password(password))
        # A new session ID at every sign-in: an ID planted before sign-in is never promoted.
        old = request.cookies.get("__Host-scc_session")
        if old:
            await store.delete_session(conn, old)
        session_id, _ = await store.create_session(conn, user.id, now, d.config)
    log.info("signed in", extra={"user_id": user.id})
    response = RedirectResponse(next_path, status_code=303)
    set_session_cookie(
        response, session_id, d.config["session"]["absolute_hours"] * 3600
    )
    response.delete_cookie(
        PRE_CSRF_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
    )
    return response


@router.post("/sign-out")
async def sign_out(request: Request) -> Response:
    session = await current_session(request)
    form = await request.form()
    if session and tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        async with deps(request).pool.connection() as conn:
            await store.delete_session(conn, request.cookies["__Host-scc_session"])
    response = RedirectResponse("/sign-in", status_code=303)
    clear_session_cookie(response)
    return response


@router.get("/account")
async def account(request: Request) -> Response:
    await require_session(request)
    return render(request, "account.html")


@router.post("/account/password")
async def change_password(request: Request) -> Response:
    session = await require_session(request)
    d = deps(request)
    form = await request.form()
    if not tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        return render(request, "account.html", 400, error=EXPIRED)
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    if not passwords.verify(session.user.password_hash, current):
        return render(
            request, "account.html", 400, error="Your current password isn't right."
        )
    if new != str(form.get("confirm_password", "")):
        return render(
            request, "account.html", 400, error="The two new passwords don't match."
        )
    problem = passwords.check_new_password(new, d.config)
    if problem:
        return render(request, "account.html", 400, error=problem)
    async with d.pool.connection() as conn:
        await store.set_password(conn, session.user.id, passwords.hash_password(new))
        # Every other session ends; this browser gets a fresh one.
        await store.delete_user_sessions(conn, session.user.id)
        session_id, csrf = await store.create_session(
            conn, session.user.id, d.clock(), d.config
        )
    log.info("password changed", extra={"user_id": session.user.id})
    request.state.session = store.Session(session.user, csrf)
    response = render(request, "account.html", done=True)
    set_session_cookie(
        response, session_id, d.config["session"]["absolute_hours"] * 3600
    )
    return response
