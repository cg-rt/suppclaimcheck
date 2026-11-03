"""Admin pages (P3.08-P3.10): jobs and traces, errors, costs, evals, health, config and users.

Admin answers only on the admin hostname (`SCC_ADMIN_HOST`), which Cloudflare Access guards (D-027),
and only to a signed-in account with the admin role. Anywhere else, /admin is a plain 404. Behind the
tunnel, a request without the Access token header is also a 404: if the Access application is ever
removed, admin closes rather than falling back to the password alone. Core and gateway data come
from their read-only admin endpoints (scc_web.admin_client); web reads only its own `auth` schema.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from psycopg.rows import dict_row

from scc_common import config_admin
from scc_common.version import version_info
from scc_web import admin, admin_views, config
from scc_web.admin_client import AdminError
from scc_web.auth import store
from scc_web.context import deps, render, require_session
from scc_web.security import tokens_match

log = logging.getLogger("scc.admin")
router = APIRouter(prefix="/admin")

SECTIONS = [
    ("overview", "Overview", "/admin"),
    ("jobs", "Jobs", "/admin/jobs"),
    ("errors", "Errors", "/admin/errors"),
    ("costs", "Costs", "/admin/costs"),
    ("evals", "Evals", "/admin/evals"),
    ("config", "Config", "/admin/config"),
    ("users", "Users", "/admin/users"),
    ("health", "Health", "/admin/health"),
]
# Config stores per module (D-048): the URL name, who serves it, and its name on the page.
CONFIG_MODULES = {
    "core": ("core-api", "Pipeline (core)"),
    "gateway": ("gateway", "Gateway"),
    "web": (None, "Web"),
}
PAGE_SIZE = 50
# A worker that hasn't beaten its heartbeat this long is treated as gone (Procrastinate beats every 10 s).
WORKER_STALE_S = 60


class NotAdmin(Exception):
    """Signed in, on the admin hostname, but the account isn't an admin."""


async def admin_session(request: Request) -> store.Session:
    d = deps(request)
    host = (request.url.hostname or "").lower()
    if d.admin is None or not d.admin_host or host != d.admin_host.lower():
        raise HTTPException(404)
    if d.trust_cloudflare and not request.headers.get("cf-access-jwt-assertion"):
        log.warning("admin request without a Cloudflare Access token")
        raise HTTPException(404)
    session = await require_session(request)
    if session.user.role != "admin":
        log.warning("admin refused: not an admin", extra={"user_id": session.user.id})
        raise NotAdmin()
    return session


def page(request: Request, name: str, section: str, status_code: int = 200, **context):
    return render(
        request,
        f"admin/{name}",
        status_code,
        section=section,
        sections=SECTIONS,
        views=admin_views,
        **context,
    )


def mount(app: FastAPI) -> None:
    """Adds the admin routes and their two error pages to web."""
    app.include_router(router)

    @app.exception_handler(NotAdmin)
    async def not_admin(request: Request, exc: NotAdmin) -> Response:
        return render(
            request,
            "error.html",
            403,
            title="This account can't open admin",
            message="Sign out and sign in with an admin account.",
        )

    @app.exception_handler(AdminError)
    async def module_down(request: Request, exc: AdminError) -> Response:
        log.warning(
            "admin page failed",
            extra={"target": exc.module, "status": exc.status, "code": exc.code},
        )
        return page(request, "unavailable.html", "", 503, error=exc)


async def _accounts(request: Request) -> list[dict]:
    async with (
        deps(request).pool.connection() as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            """
            SELECT u.id, u.email, u.role, u.created_at, u.disabled_at,
                   (SELECT max(a.at) FROM auth.login_attempts a
                     WHERE a.email_lower = lower(u.email) AND a.succeeded) AS last_sign_in,
                   (SELECT count(*) FROM auth.sessions s
                     WHERE s.user_id = u.id AND s.expires_at > now()) AS sessions
            FROM auth.users u ORDER BY u.created_at, u.id
            """
        )
        return await cur.fetchall()


async def _emails(request: Request) -> dict[int, str]:
    return {a["id"]: a["email"] for a in await _accounts(request)}


async def _errors(request: Request, limit: int) -> list[dict]:
    """Core's recent failures and the gateway's failed LLM calls, newest first."""
    d = deps(request)
    core, calls = await asyncio.gather(
        d.admin.core_errors(limit), d.admin.llm_calls(status="failed", limit=limit)
    )
    items = [{**e, "module": "core"} for e in core]
    items += [
        {
            "kind": "llm_failed",
            "at": c["created_at"],
            "job_id": c["job_id"],
            "code": admin_views.code_of(c["error"]) or "unknown",
            "detail": f"{c['stage']} · {c['model']}",
            "correlation_id": c["correlation_id"],
            "module": "gateway",
        }
        for c in calls
    ]
    items.sort(key=lambda e: e["at"], reverse=True)
    return items[:limit]


async def _health(request: Request) -> list[dict]:
    """One row per module: up or not, what it checked, and the version it runs."""
    d = deps(request)

    async def database() -> dict:
        try:
            async with d.pool.connection() as conn:
                server = await (await conn.execute("SHOW server_version")).fetchone()
                vector = await (
                    await conn.execute(
                        "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                    )
                ).fetchone()
            return {
                "ok": True,
                "server": server[0],
                "vector": vector[0] if vector else None,
            }
        except Exception as exc:  # noqa: BLE001 - any failure means the database is not usable
            return {"ok": False, "error": type(exc).__name__}

    async def module(name: str) -> dict:
        body, status = await d.admin.health(name)
        try:
            info = await d.admin.version(name)
        except AdminError:
            info = {}
        return {"body": body, "status": status, "info": info}

    async def queue() -> dict | None:
        try:
            return await d.admin.queue()
        except AdminError:
            return None

    db, core, gateway, tasks = await asyncio.gather(
        database(), module("core-api"), module("gateway"), queue()
    )
    web = version_info("web")
    rows = [
        {
            "name": "web",
            "ok": db["ok"],
            "detail": "database ok" if db["ok"] else f"database: {db['error']}",
            "version": web["version"],
            "git_sha": web["git_sha"],
            "rb": "RB-04" if not db["ok"] else None,
        }
    ]
    for name, result in (("core-api", core), ("gateway", gateway)):
        body = result["body"]
        checks = body.get("checks") or {}
        detail = ", ".join(f"{k} {v}" for k, v in checks.items()) or body.get(
            "error", ""
        )
        ok = result["status"] == 200 and body.get("status") == "ok"
        rows.append(
            {
                "name": name,
                "ok": ok,
                "detail": detail if result["status"] else f"unreachable ({detail})",
                "version": result["info"].get("version"),
                "git_sha": result["info"].get("git_sha"),
                "rb": None
                if ok
                else ("RB-04" if checks.get("database") == "failed" else "RB-03"),
            }
        )
    if tasks is None:
        worker = {"ok": False, "detail": "core-api could not report the queue"}
    else:
        live = [w for w in tasks["workers"] if w["seconds_since"] < WORKER_STALE_S]
        counts = (
            ", ".join(f"{k} {v}" for k, v in sorted(tasks["tasks"].items())) or "empty"
        )
        beat = (
            f"last heartbeat {min(w['seconds_since'] for w in live):.0f} s ago"
            if live
            else "no heartbeat in the last minute"
        )
        worker = {
            "ok": bool(live),
            "detail": f"{len(live)} worker(s), {beat}; queue: {counts}",
        }
    rows.append(
        {
            "name": "core-worker",
            "version": core["info"].get("version"),
            "git_sha": core["info"].get("git_sha"),
            "note": "same image as core-api",
            "rb": None if worker["ok"] else "RB-03",
            **worker,
        }
    )
    rows.append(
        {
            "name": "postgres",
            "ok": db["ok"],
            "detail": (
                f"PostgreSQL {db['server']}, pgvector {db['vector'] or 'missing'}"
                if db["ok"]
                else f"web can't connect: {db['error']}"
            ),
            "version": None,
            "git_sha": None,
            "rb": None if db["ok"] else "RB-04",
        }
    )
    tunnel = bool(request.headers.get("cf-connecting-ip"))
    rows.append(
        {
            "name": "edge",
            "ok": True if tunnel else None,
            "detail": (
                "this page came through the Cloudflare Tunnel"
                if tunnel
                else "not on this path: this page was opened without the tunnel"
            ),
            "version": None,
            "git_sha": None,
            "rb": None,
        }
    )
    return rows


@router.get("")
async def overview(request: Request) -> Response:
    await admin_session(request)
    d = deps(request)
    jobs, errors, usage, health = await asyncio.gather(
        d.admin.jobs(limit=1),
        _errors(request, 10),
        d.admin.usage(days=1),
        _health(request),
    )
    since = datetime.now(UTC) - timedelta(hours=24)
    recent = [e for e in errors if datetime.fromisoformat(e["at"]) >= since]
    return page(
        request,
        "overview.html",
        "overview",
        counts=jobs["counts"],
        errors=errors[:5],
        errors_24h=len(recent),
        lanes=usage["lanes"],
        unhealthy=[r for r in health if r["ok"] is False],
    )


@router.get("/jobs")
async def jobs_list(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    page_number: Annotated[int, Query(alias="page")] = 1,
) -> Response:
    await admin_session(request)
    d = deps(request)
    status = status if status in admin_views.STATUSES else None
    q = (q or "").strip()[:200]
    page_number = max(1, min(page_number, 2000))
    emails = await _emails(request)
    params = {
        "status": status,
        "limit": PAGE_SIZE,
        "offset": (page_number - 1) * PAGE_SIZE,
    }
    if "@" in q:
        # An email (or part of one) finds that user's jobs; web resolves it, core only knows user IDs.
        params["user_id"] = [
            i for i, e in emails.items() if q.lower() in e.lower()
        ] or [0]
    elif q:
        params["q"] = q
    listing = await d.admin.jobs(**params)
    return page(
        request,
        "jobs.html",
        "jobs",
        listing=listing,
        status=status,
        q=q,
        page_number=page_number,
        emails=emails,
    )


def _job_id(job_id: str) -> str:
    try:
        return str(uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(404) from None


async def _trace(request: Request, job_id: str) -> dict:
    try:
        return await deps(request).admin.trace(job_id)
    except AdminError as exc:
        if exc.status == 404:
            raise HTTPException(404) from None
        raise


@router.get("/jobs/{job_id}")
async def job_trace(
    request: Request, job_id: str, retried: str | None = None
) -> Response:
    await admin_session(request)
    d = deps(request)
    job_id = _job_id(job_id)
    trace = await _trace(request, job_id)
    calls, pipeline, emails = await asyncio.gather(
        d.admin.llm_calls(job_id=job_id, limit=500),
        d.admin.config_version("core-api", trace["job"]["config_version"]),
        _emails(request),
        return_exceptions=True,
    )
    if isinstance(emails, BaseException):
        raise emails
    job = trace["job"]
    result = job["result"] or {}
    quoted = {q["passage_id"] for q in result.get("quotes", [])}
    calls = None if isinstance(calls, BaseException) else calls
    # Created under the submission's ID; the search runs under the confirmation's (and a retry's).
    correlation_ids = list(
        dict.fromkeys(
            [job["correlation_id"] or job["id"]]
            + [c["correlation_id"] for c in calls or [] if c["correlation_id"]]
        )
    )
    return page(
        request,
        "trace.html",
        "jobs",
        trace=trace,
        job=job,
        result=result,
        quoted=quoted,
        email=emails.get(job["user_id"]),
        calls=calls,
        correlation_ids=correlation_ids,
        pipeline=None if isinstance(pipeline, BaseException) else pipeline,
        failure=admin_views.explain(trace["failure_code"]),
        retried=retried,
        log_command=admin_views.log_command(correlation_ids),
    )


@router.post("/jobs/{job_id}/retry")
async def job_retry(request: Request, job_id: str) -> Response:
    session = await admin_session(request)
    job_id = _job_id(job_id)
    form = await request.form()
    if not tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        return RedirectResponse(
            f"/admin/jobs/{job_id}?retried=expired", status_code=303
        )
    try:
        job = await deps(request).admin.retry(job_id, session.user.id)
    except AdminError as exc:
        if exc.status == 404:
            raise HTTPException(404) from None
        if exc.status == 409:
            return RedirectResponse(
                f"/admin/jobs/{job_id}?retried=not-failed", status_code=303
            )
        raise
    log.info(
        "admin retried a job",
        extra={"job_id": job_id, "actor_user_id": session.user.id},
    )
    return RedirectResponse(
        f"/admin/jobs/{job_id}?retried={job['state']}", status_code=303
    )


@router.get("/jobs/{job_id}/passages/{pmcid}")
async def job_passages(request: Request, job_id: str, pmcid: str) -> Response:
    await admin_session(request)
    job_id = _job_id(job_id)
    trace = await _trace(request, job_id)
    try:
        passages = await deps(request).admin.passages(job_id, pmcid)
    except AdminError as exc:
        if exc.status in (404, 422):
            raise HTTPException(404) from None
        raise
    result = trace["job"]["result"] or {}
    quotes = {
        q["passage_id"]: q for q in result.get("quotes", []) if q["pmcid"] == pmcid
    }
    dropped = {d["passage_ref"] for d in trace["dropped_quotes"]}
    # The passages the model read, recorded on jobs that reach a result; older jobs don't have it.
    retrieved = result.get("retrieved")
    article = next(
        (a for a in result.get("articles", []) if a.get("pmcid") == pmcid), None
    )
    return page(
        request,
        "passages.html",
        "jobs",
        job=trace["job"],
        pmcid=pmcid,
        article=article,
        passages=passages,
        quotes=quotes,
        dropped=dropped,
        retrieved=None if retrieved is None else set(retrieved.get(pmcid, [])),
    )


@router.get("/errors")
async def errors(request: Request) -> Response:
    await admin_session(request)
    return page(request, "errors.html", "errors", errors=await _errors(request, 100))


@router.get("/costs")
async def costs(request: Request) -> Response:
    await admin_session(request)
    d = deps(request)
    usage, calls = await asyncio.gather(
        d.admin.usage(days=14), d.admin.llm_calls(limit=50)
    )
    return page(request, "costs.html", "costs", usage=usage, calls=calls)


@router.get("/evals")
async def evals(request: Request) -> Response:
    await admin_session(request)
    d = deps(request)
    pipeline = await d.admin.config("core-api")
    with_results = [v["version"] for v in pipeline["versions"] if v["latest_eval"]]
    details = await asyncio.gather(
        *(d.admin.config_version("core-api", v) for v in with_results)
    )
    runs = {detail["version"]: detail["evals"] for detail in details}
    return page(request, "evals.html", "evals", pipeline=pipeline, runs=runs)


async def _store(request: Request, module: str) -> dict:
    served_by, _ = CONFIG_MODULES[module]
    if served_by:
        return await deps(request).admin.config(served_by)
    async with deps(request).pool.connection() as conn:
        return (await config_admin.overview(conn, config.STORE)).model_dump(mode="json")


async def _version(request: Request, module: str, version: str) -> dict | None:
    served_by, _ = CONFIG_MODULES[module]
    if served_by:
        try:
            return await deps(request).admin.config_version(served_by, version)
        except AdminError as exc:
            if exc.status in (404, 422):
                return None
            raise
    async with deps(request).pool.connection() as conn:
        found = await config_admin.version(conn, config.STORE, version)
    return found.model_dump(mode="json") if found else None


@router.get("/config")
async def config_overview(request: Request) -> Response:
    await admin_session(request)
    stores = await asyncio.gather(*(_store(request, m) for m in CONFIG_MODULES))
    return page(
        request,
        "config.html",
        "config",
        stores=[
            {"slug": slug, "label": label, **found}
            for (slug, (_, label)), found in zip(
                CONFIG_MODULES.items(), stores, strict=True
            )
        ],
    )


@router.get("/config/{module}/{version}")
async def config_version(
    request: Request, module: str, version: str, against: str | None = None
) -> Response:
    await admin_session(request)
    if module not in CONFIG_MODULES:
        raise HTTPException(404)
    overview_, found = await asyncio.gather(
        _store(request, module), _version(request, module, version)
    )
    if found is None:
        raise HTTPException(404)
    names = [v["version"] for v in overview_["versions"]]
    if against not in names or against == version:
        position = names.index(version)
        against = names[position - 1] if position > 0 else None
    base = await _version(request, module, against) if against else None
    return page(
        request,
        "config_version.html",
        "config",
        module=module,
        label=CONFIG_MODULES[module][1],
        found=found,
        names=names,
        against=against,
        diff=admin_views.diff_lines(
            base["body_toml"] if base else None,
            found["body_toml"],
            against or "(nothing)",
            version,
        ),
    )


async def _users_page(request: Request, status_code: int = 200, **context) -> Response:
    session = await admin_session(request)
    return page(
        request,
        "users.html",
        "users",
        status_code,
        accounts=await _accounts(request),
        me=session.user.id,
        **context,
    )


@router.get("/users")
async def users(request: Request) -> Response:
    return await _users_page(request)


async def _account_form(request: Request) -> tuple[store.Session, dict | None]:
    """The admin and the posted form, or no form when its CSRF token doesn't match."""
    session = await admin_session(request)
    form = await request.form()
    if not tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        return session, None
    return session, {k: str(v) for k, v in form.items()}


@router.post("/users")
async def create_user(request: Request) -> Response:
    session, form = await _account_form(request)
    if form is None:
        return await _users_page(request, 400, error="This form expired. Try again.")
    email, role = form.get("email", "").strip(), form.get("role", "user")
    password = admin.new_password()
    try:
        async with deps(request).pool.connection() as conn:
            user_id = await admin.create_account(
                conn, email, role, password, deps(request).config
            )
    except admin.AccountError as exc:
        return await _users_page(request, 400, error=str(exc), email=email, role=role)
    log.info(
        "admin created an account",
        extra={"actor_user_id": session.user.id, "user_id": user_id, "role": role},
    )
    return await _users_page(
        request, shown={"email": email, "password": password, "new": True}
    )


async def _target(request: Request, user_id: int) -> store.User:
    account = next((a for a in await _accounts(request) if a["id"] == user_id), None)
    if account is None:
        raise HTTPException(404)
    async with deps(request).pool.connection() as conn:
        return await store.user_by_email(conn, account["email"])


@router.post("/users/{user_id}/reset")
async def reset_user(request: Request, user_id: int) -> Response:
    session, form = await _account_form(request)
    if form is None:
        return await _users_page(request, 400, error="This form expired. Try again.")
    if user_id == session.user.id:
        return await _users_page(
            request, 400, error="Change your own password under Account instead."
        )
    user = await _target(request, user_id)
    password = admin.new_password()
    async with deps(request).pool.connection() as conn:
        await admin.reset_password(conn, user, password, deps(request).config)
    log.info(
        "admin reset a password",
        extra={"actor_user_id": session.user.id, "user_id": user.id},
    )
    return await _users_page(request, shown={"email": user.email, "password": password})


@router.post("/users/{user_id}/disable")
async def disable_user(request: Request, user_id: int) -> Response:
    session, form = await _account_form(request)
    if form is None:
        return await _users_page(request, 400, error="This form expired. Try again.")
    if user_id == session.user.id:
        return await _users_page(
            request, 400, error="You can't disable the account you are signed in with."
        )
    user = await _target(request, user_id)
    async with deps(request).pool.connection() as conn:
        await admin.disable_account(conn, user)
    log.info(
        "admin disabled an account",
        extra={"actor_user_id": session.user.id, "user_id": user.id},
    )
    return await _users_page(
        request, done=f"{user.email} is disabled and signed out everywhere."
    )


@router.get("/health")
async def health(request: Request) -> Response:
    await admin_session(request)
    rows = await _health(request)
    versions = {r["version"] for r in rows if r["version"]}
    return page(request, "health.html", "health", rows=rows, mixed=len(versions) > 1)
