"""The signed-in pages (P1.10): new check (text, or a link to a page, P3.12), confirm, live progress,
result, history; the feedback form under a result (P4.03); and three pages anyone can read: the privacy
notice (P3.06), what the page reader does (P3.12) and how a check works (P3.11)."""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from scc_web import views
from scc_web.context import current_session, deps, render, require_session, templates
from scc_web.core_client import CoreError
from scc_web.security import tokens_match

log = logging.getLogger("scc.pages")
router = APIRouter()

CLAIM_FIELDS = ("supplement", "effect", "population", "dose", "duration")
MAX_INPUT = 20_000
# A link (P3.12): one http or https address, as core accepts it. The gateway checks where it leads.
MAX_LINK = 2048
LINK = re.compile(r"^https?://[^\s/?#@]+(?:[/?#]\S*)?$", re.IGNORECASE)


@router.get("/")
async def home(request: Request) -> Response:
    target = "/checks/new" if await current_session(request) else "/sign-in"
    return RedirectResponse(target, status_code=303)


async def _links_on(request: Request) -> bool:
    """Whether core's active config lets a check start from a link (P3.12). Off if core can't say."""
    try:
        return bool((await deps(request).core.input_types()).get("url"))
    except (CoreError, httpx.HTTPError) as exc:
        log.warning("input types unknown", extra={"error": type(exc).__name__})
        return False


async def _new_check_page(
    request: Request, status_code: int = 200, mode: str = "text", **context
) -> Response:
    links = await _links_on(request)
    # A fresh key per form: submitting the same form twice creates one job, not two.
    return render(
        request,
        "new_check.html",
        status_code,
        idempotency_key=str(uuid.uuid4()),
        method=METHOD,
        links=links,
        mode=mode if links else "text",
        **context,
    )


# Refusals whose message core wrote for the user (P3.02, P3.04): shown as they are, with core's status.
USER_FACING = {
    "injection_throttled",
    "budget_cap_reached",
    "daily_quota_reached",
    "budget_unavailable",
    "input_disabled",
}


@router.get("/checks/new")
async def new_check(
    request: Request, start_from: str | None = Query(None, alias="from")
) -> Response:
    await require_session(request)
    return await _new_check_page(
        request, mode="link" if start_from == "link" else "text"
    )


@router.post("/checks")
async def create_check(request: Request) -> Response:
    session = await require_session(request)
    form = await request.form()
    if str(form.get("input_type", "text")) == "url":
        return await _create_from_link(request, session, form)
    text = str(form.get("input_text", "")).strip()
    if not tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        return await _new_check_page(
            request,
            400,
            text=text,
            error="This form expired. Your text is still here: send it again.",
        )
    if not text:
        return await _new_check_page(
            request, 400, error="Paste or type the claim you want to check."
        )
    if len(text) > MAX_INPUT:
        return await _new_check_page(
            request,
            400,
            text=text,
            error=f"That's more than {MAX_INPUT:,} characters. Paste the part with the claim.",
        )
    return await _start(request, session, form, text, "text")


def link_from(typed: str) -> str | None:
    """The address as typed, with https:// added when it has no scheme; None if it isn't one address."""
    link = typed.strip()
    if link and "://" not in link and not link.lower().startswith(("http:", "https:")):
        link = "https://" + link
    return link if len(link) <= MAX_LINK and LINK.match(link) else None


async def _create_from_link(request: Request, session, form) -> Response:
    typed = str(form.get("input_url", "")).strip()
    if not tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        return await _new_check_page(
            request,
            400,
            mode="link",
            url=typed,
            error="This form expired. Your link is still here: send it again.",
        )
    if not typed:
        return await _new_check_page(
            request, 400, mode="link", error="Paste the address of the page to check."
        )
    link = link_from(typed)
    if link is None:
        return await _new_check_page(
            request,
            400,
            mode="link",
            url=typed,
            error="That isn't a web address. Copy the whole address from your browser's address "
            "bar, starting with https://",
        )
    return await _start(request, session, form, link, "url")


async def _start(
    request: Request, session, form, text: str, input_type: str
) -> Response:
    """Creates the job and goes to its page; a refusal shows the form again with what was typed."""
    key = str(form.get("idempotency_key", "")) or str(uuid.uuid4())
    kept = {"url": text, "mode": "link"} if input_type == "url" else {"text": text}
    try:
        job = await deps(request).core.create_job(
            session.user.id, text, f"web-{key}", input_type
        )
    except CoreError as exc:
        log.warning("create job failed", extra={"code": exc.code})
        if exc.code in USER_FACING and exc.message:
            # Core's message says what happened and when checks work again.
            return await _new_check_page(request, exc.status, error=exc.message, **kept)
        return await _new_check_page(
            request,
            503,
            error="The check could not be started. Please try again in a minute.",
            **kept,
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


@router.get("/privacy")
async def privacy(request: Request) -> Response:
    await current_session(
        request
    )  # for the header when signed in; the page itself is public
    d = deps(request)
    return render(
        request,
        "privacy.html",
        p=d.config["privacy"],
        login_days=d.config["retention"]["login_attempts_days"],
        session_rules=d.config["session"],
        operator=d.operator,
    )


FEEDBACK_CHOICES = {
    "verdict_right": ("yes", "no", "unsure"),
    "understandable": ("yes", "partly", "no"),
}
FEEDBACK_MAX = 1000


@router.post("/checks/{job_id}/feedback")
async def save_feedback(request: Request, job_id: str) -> Response:
    """Stores the person's answers about their own result (P4.03), then shows the result again."""
    session = await require_session(request)
    job = await _own_job(request, job_id)
    if job["state"] != "done":
        raise HTTPException(404)
    form = await request.form()

    def again(message: str, status_code: int = 400) -> Response:
        return render(
            request,
            "result.html",
            status_code,
            job=job,
            view=views.result_view(job),
            feedback_error=message,
        )

    if not tokens_match(session.csrf_token, str(form.get("csrf_token", ""))):
        return again("This form expired. Choose your answers again and save.")
    answers = {
        name: (value if (value := str(form.get(name, ""))) in choices else None)
        for name, choices in FEEDBACK_CHOICES.items()
    }
    missing = str(form.get("missing", "")).strip()
    if len(missing) > FEEDBACK_MAX:
        return again(f"Keep the last answer under {FEEDBACK_MAX} characters.")
    answers["missing"] = missing or None
    if not any(answers.values()):
        return again("Answer at least one question, then save.")
    try:
        await deps(request).core.save_feedback(job_id, session.user.id, answers)
    except CoreError as exc:
        log.warning("feedback not saved", extra={"code": exc.code})
        return again(
            "Your answers could not be saved. Please try again in a minute.", 503
        )
    return RedirectResponse(
        f"/checks/{job_id}?feedback=saved#feedback", status_code=303
    )


@router.get("/checks/{job_id}")
async def show_check(
    request: Request,
    job_id: str,
    spelling: int | None = None,
    feedback: str | None = None,
) -> Response:
    job = await _own_job(request, job_id)
    state = job["state"]
    if state == "awaiting_confirmation":
        return render(
            request,
            "confirm.html",
            job=job,
            claims=views.with_spelling(job["extracted_claims"] or [], spelling),
        )
    if state == "done":
        return render(
            request,
            "result.html",
            job=job,
            view=views.result_view(job),
            feedback_saved=feedback == "saved",
        )
    if state in {"failed", "refused", "needs_clarification"}:
        return render(request, "stopped.html", job=job)
    events = await deps(request).core.job_events(job_id)
    names = views.stage_names(job)
    return render(
        request,
        "progress.html",
        job=job,
        rows=views.progress(state, events, names),
        stages=names,
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
            known = exc.code in USER_FACING and exc.message
            return render(
                request,
                "confirm.html",
                exc.status if known else 503,
                job=job,
                claims=[claim],
                error=exc.message
                if known
                else "The search could not be started. Please try again in a minute.",
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
                names = views.stage_names(job)
                rows = views.progress(last, events, names)
                yield _sse(
                    "progress",
                    templates.get_template("partials/stages.html").render(rows=rows),
                )
                yield _sse("status", names.get(last, last))
            if last in views.SETTLED:
                yield _sse("settled", "")
                return
            await asyncio.sleep(cfg["poll_seconds"])

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@router.get("/reader")
async def reader(request: Request) -> Response:
    """What the page reader does and how a site can say no (P3.12): the page its user agent names."""
    await current_session(
        request
    )  # for the header when signed in; the page itself is public
    return render(request, "reader.html", reader=READER)


# The page reader's manners as the gateway's config sets them (gateway-v3 [pages]); a test keeps the
# two equal, since web doesn't read the gateway's config.
READER = {
    "token": "SuppClaimCheck",
    "user_agent": "SuppClaimCheck (+https://suppclaimcheck.com/reader)",
    "timeout_s": 10,
    "max_mb": 2,
    "min_interval_s": 1,
    "robots_cache_minutes": 60,
}


@router.get("/method")
async def method(request: Request) -> Response:
    """How a check reaches a verdict (P3.11, docs/METHOD.md): public, so it can be read before an
    invitation. The numbers are METHOD's, which a test keeps equal to core's active config."""
    await current_session(
        request
    )  # for the header when signed in; the page itself is public
    m = METHOD
    tiers = [
        (views.TIER_NAMES[t], w, t in m["human_tiers"]) for t, w in m["weights"].items()
    ]
    return render(request, "method.html", m=m, tiers=tiers, example=worked_example(m))


# The method as core's active config sets it (pipeline-v9 [search], [retrieval], [evidence], [rubric],
# [cache], [inputs]). Web doesn't read core's config, so a test keeps the two equal, like READER.
METHOD = {
    "pipeline": "pipeline-v9",
    "rubric": "rubric-v1",
    "considered": 20,
    "read": 5,
    "passages": 6,
    "keyword_weight": 0.7,
    "weights": {
        "systematic_review": 1.0,
        "rct": 0.8,
        "human_trial": 0.6,
        "observational": 0.4,
        "human_other": 0.4,
        "narrative_review": 0.3,
        "unclassified": 0.2,
        "animal": 0.15,
        "in_vitro": 0.1,
        "other": 0.1,
    },
    "human_tiers": (
        "systematic_review",
        "rct",
        "human_trial",
        "observational",
        "human_other",
        "narrative_review",
    ),
    "full_strength": 2.0,
    "min_strength": 0.25,
    "mixed_share": 0.25,
    "reuse_days": 7,
    "page_chars": 30_000,
}


def worked_example(m: dict) -> dict:
    """The method page's example, worked out from the current weights and thresholds: a meta-analysis
    and a randomised trial support the claim, a narrative review contradicts it."""
    w_for = m["weights"]["systematic_review"] + m["weights"]["rct"]
    w_against = m["weights"]["narrative_review"]
    total = w_for + w_against
    direction = (w_for - w_against) / total
    strength = min(1.0, total / m["full_strength"])
    share = min(w_for, w_against) / total
    return {
        "w_for": w_for,
        "w_against": w_against,
        "total": total,
        "share": share,
        "mixed": share >= m["mixed_share"],
        "direction": direction,
        "strength": strength,
        "score": round(50 + 50 * direction * strength),
    }


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
