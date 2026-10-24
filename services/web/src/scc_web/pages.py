"""The signed-in pages (P1.10): new check, confirm, live progress, result, history."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from scc_web import views
from scc_web.context import current_session, deps, render, require_session, templates
from scc_web.core_client import CoreError
from scc_web.security import tokens_match

log = logging.getLogger("scc.pages")
router = APIRouter()

CLAIM_FIELDS = ("supplement", "effect", "population", "dose", "duration")
MAX_INPUT = 20_000


@router.get("/")
async def home(request: Request) -> Response:
    target = "/checks/new" if await current_session(request) else "/sign-in"
    return RedirectResponse(target, status_code=303)


def _new_check_page(request: Request, status_code: int = 200, **context) -> Response:
    # A fresh key per form: submitting the same form twice creates one job, not two.
    return render(
        request,
        "new_check.html",
        status_code,
        idempotency_key=str(uuid.uuid4()),
        **context,
    )


@router.get("/checks/new")
async def new_check(request: Request) -> Response:
    await require_session(request)
    return _new_check_page(request)


@router.post("/checks")
async def create_check(request: Request) -> Response:
    session = await require_session(request)
    form = await request.form()
    text = str(form.get("input_text", "")).strip()
    if not tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        return _new_check_page(
            request,
            400,
            text=text,
            error="This form expired. Your text is still here: send it again.",
        )
    if not text:
        return _new_check_page(
            request, 400, error="Paste or type the claim you want to check."
        )
    if len(text) > MAX_INPUT:
        return _new_check_page(
            request,
            400,
            text=text,
            error=f"That's more than {MAX_INPUT:,} characters. Paste the part with the claim.",
        )
    key = str(form.get("idempotency_key", "")) or str(uuid.uuid4())
    try:
        job = await deps(request).core.create_job(session.user.id, text, f"web-{key}")
    except CoreError as exc:
        log.warning("create job failed", extra={"code": exc.code})
        return _new_check_page(
            request,
            503,
            text=text,
            error="The check could not be started. Please try again in a minute.",
        )
    return RedirectResponse(f"/checks/{job['id']}", status_code=303)


async def _own_job(request: Request, job_id: str) -> dict:
    """The job, if it exists and belongs to the signed-in user; otherwise a plain 404."""
    session = await require_session(request)
    try:
        uuid.UUID(job_id)
        job = await deps(request).core.get_job(job_id)
    except ValueError, CoreError:
        raise HTTPException(404) from None
    if job["user_id"] != session.user.id:
        raise HTTPException(404)
    return job


@router.get("/checks/{job_id}")
async def show_check(request: Request, job_id: str) -> Response:
    job = await _own_job(request, job_id)
    state = job["state"]
    if state == "awaiting_confirmation":
        return render(
            request, "confirm.html", job=job, claims=job["extracted_claims"] or []
        )
    if state == "done":
        return render(request, "result.html", job=job, view=views.result_view(job))
    if state in {"failed", "refused"}:
        return render(request, "stopped.html", job=job)
    events = await deps(request).core.job_events(job_id)
    return render(
        request,
        "progress.html",
        job=job,
        rows=views.progress(state, events),
        stages=views.STAGES,
    )


@router.post("/checks/{job_id}/confirm")
async def confirm_check(request: Request, job_id: str) -> Response:
    session = await require_session(request)
    job = await _own_job(request, job_id)
    form = await request.form()
    claim = {name: (str(form.get(name, "")).strip() or None) for name in CLAIM_FIELDS}
    if not tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        return render(
            request,
            "confirm.html",
            400,
            job=job,
            claims=[claim],
            error="This form expired. Check the claim and confirm again.",
        )
    if not claim["supplement"] or not claim["effect"]:
        return render(
            request,
            "confirm.html",
            400,
            job=job,
            claims=[claim],
            error="Fill in the supplement and the effect: the search needs both.",
        )
    try:
        await deps(request).core.confirm(job_id, claim)
    except CoreError as exc:
        if (
            exc.status != 409
        ):  # 409: already confirmed, so showing the job is the right answer
            log.warning("confirm failed", extra={"code": exc.code})
            return render(
                request,
                "confirm.html",
                503,
                job=job,
                claims=[claim],
                error="The search could not be started. Please try again in a minute.",
            )
    return RedirectResponse(f"/checks/{job_id}", status_code=303)


def _sse(event: str, html: str) -> str:
    data = "\n".join(f"data: {line}" for line in html.splitlines() or [""])
    return f"event: {event}\n{data}\n\n"


@router.get("/checks/{job_id}/events")
async def check_events(request: Request, job_id: str) -> StreamingResponse:
    """Live progress (server-sent events): the stage list each time it changes, then `settled`."""
    await _own_job(request, job_id)
    d = deps(request)
    cfg = d.config["progress"]

    async def stream() -> AsyncIterator[str]:
        last = None
        deadline = time.monotonic() + cfg["max_minutes"] * 60
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                return
            try:
                job = await d.core.get_job(job_id)
                events = await d.core.job_events(job_id)
            except CoreError:
                await asyncio.sleep(cfg["poll_seconds"] * 3)
                continue
            if job["state"] != last:
                last = job["state"]
                rows = views.progress(last, events)
                yield _sse(
                    "progress",
                    templates.get_template("partials/stages.html").render(rows=rows),
                )
                yield _sse("status", views.STAGES.get(last, last))
            if last in views.SETTLED:
                yield _sse("settled", "")
                return
            await asyncio.sleep(cfg["poll_seconds"])

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@router.get("/history")
async def history(request: Request) -> Response:
    session = await require_session(request)
    jobs = await deps(request).core.list_jobs(session.user.id)
    return render(
        request,
        "history.html",
        jobs=jobs,
        classes=views.VERDICT_CLASS,
        stages=views.STAGES,
    )
