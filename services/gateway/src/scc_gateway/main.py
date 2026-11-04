"""The gateway: the only module that calls outside services (LLM, literature, alerts; D-011, D-033).

Internal only. Holds every API key, so the budget cap can't be bypassed (P1.06, P1.07).
"""

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Literal

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from scc_common import config_admin
from scc_common.app import Check, create_app
from scc_common.db import conninfo_from_env, db_check, read_secret
from scc_gateway import admin_api, budget, config
from scc_gateway.alerts import AlertFailed, Alerts, AlertsNotConfigured
from scc_gateway.literature import Literature, LiteratureUnavailable
from scc_gateway.llm import (
    CallInProgress,
    ClaudeProvider,
    LlmFailure,
    LlmRequest,
    MissingProvider,
    Provider,
    structured_call,
)
from scc_gateway.retry import Transient

log = logging.getLogger("scc.gateway")


@dataclass
class Deps:
    pool: AsyncConnectionPool
    provider: Provider
    literature: Literature
    config: dict
    alerts: Alerts | None = field(default=None)


class StructuredRequest(BaseModel):
    stage: str = Field(pattern=r"^[a-z_]{1,32}$")
    job_id: str | None = None
    user_id: int | None = None
    lane: Literal["live", "eval"] = "live"
    model: str
    max_tokens: Annotated[int, Field(ge=1, le=16_000)]
    effort: Literal["low", "medium", "high"]
    thinking: Literal["disabled", "adaptive"]
    instructions: str = Field(min_length=1, max_length=20_000)
    data: str = Field(max_length=200_000)
    # `schema` shadows a BaseModel attribute, so the field has another name and keeps the JSON key.
    output_schema: dict = Field(alias="schema")


class StructuredReply(BaseModel):
    output: dict
    model: str
    input_tokens: int
    output_tokens: int
    cost_eur: float
    call_id: int
    # An earlier call's stored answer, returned for the same Idempotency-Key: nothing charged (P3.05).
    reused: bool = False


class LaneStatus(BaseModel):
    lane: str
    spent_eur: float
    cap_eur: float
    # ok, warn_50, warn_80 (the config's warning shares) or cap (too little left for a new check).
    level: str


class QuotaStatus(BaseModel):
    checks_today: int
    quota: int


class BudgetStatus(BaseModel):
    """Whether a new check may start (P3.04, D-053), and why not; and where each lane stands."""

    new_checks_allowed: bool
    code: str | None
    message: str | None
    lanes: list[LaneStatus]
    # The account's checks today and its quota; None without a user or a quota.
    user: QuotaStatus | None


class NotifyRequest(BaseModel):
    # safety_review and injection_throttled come from core's triage (P3.02, P3.03, D-050).
    event: Literal[
        "budget_cap_reached",
        "budget_warning",
        "job_failed",
        "safety_review",
        "injection_throttled",
        "test",
    ]
    # A code, never user text: it goes to a phone outside this system.
    code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,40}$")
    text: str = Field(min_length=1, max_length=500)


class NotifyReply(BaseModel):
    outcome: Literal["sent", "quiet", "deduplicated"]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    max_results: Annotated[int, Field(ge=1, le=100)] = 20


class SpellRequest(BaseModel):
    term: str = Field(min_length=1, max_length=200)


class SpellReply(BaseModel):
    term: str
    suggestion: str | None


class FetchRequest(BaseModel):
    pmids: list[str] = Field(max_length=100)
    full_texts: Annotated[int, Field(ge=0, le=10)] = 1


# HTTP status for each LLM failure code. 402 for the budget, so callers can tell it apart.
_STATUS = {
    "budget_cap_reached": 402,
    "daily_quota_reached": 429,
    "call_in_progress": 409,
    "unknown_model": 400,
    "provider_not_configured": 503,
    "provider_unavailable": 503,
    "provider_rejected": 502,
    "provider_refused": 422,
    "output_not_json": 422,
    "output_truncated": 422,
}

router = APIRouter()


def _deps(request: Request) -> Deps:
    return request.app.state.deps


def _error(code: str, message: str) -> HTTPException:
    return HTTPException(_STATUS.get(code, 502), {"code": code, "message": message})


# What core tells the user when a new check can't start (P3.04).
QUOTA_MESSAGE = "This account has used today's checks. You can start new checks again tomorrow (UTC)."
CAP_MESSAGE = (
    "This month's budget for checks is used up. Checks will work again next month."
)
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=128,
        pattern=r"^[A-Za-z0-9:_-]{8,128}$",
        description="The same key again returns the stored answer instead of a new, charged call",
    ),
]


@router.get("/budget/status", tags=["budget"])
async def budget_status(
    request: Request, user_id: Annotated[int | None, Query(ge=1)] = None
) -> BudgetStatus:
    """Where each lane stands this month, and whether a new live check may start for this account."""
    deps = _deps(request)
    async with deps.pool.connection() as conn:
        lanes = []
        for lane in admin_api.LANES:
            spent = await budget.month_spend(conn, lane)
            lanes.append(
                LaneStatus(
                    lane=lane,
                    spent_eur=round(spent, 6),
                    cap_eur=budget.cap_for(lane, deps.config),
                    level=budget.level(lane, spent, deps.config),
                )
            )
        quota = budget.quota_for(user_id, deps.config)
        used = await budget.checks_today(conn, user_id) if quota is not None else None
    live = next(lane for lane in lanes if lane.lane == "live")
    code = message = None
    if live.level == "cap":
        code, message = "budget_cap_reached", CAP_MESSAGE
    elif quota is not None and used >= quota:
        code, message = "daily_quota_reached", QUOTA_MESSAGE
    return BudgetStatus(
        new_checks_allowed=code is None,
        code=code,
        message=message,
        lanes=lanes,
        user=QuotaStatus(checks_today=used, quota=quota) if quota is not None else None,
    )


async def _warn(deps: Deps, lane: str) -> None:
    """A Telegram alert for each warning level the lane has newly passed this month (P3.04)."""
    try:
        async with deps.pool.connection() as conn:
            due = await budget.warnings_due(conn, lane, deps.config)
            spent = await budget.month_spend(conn, lane)
    except Exception:  # a warning must never fail the call it follows
        log.exception("budget warning check failed", extra={"lane": lane})
        return
    cap = budget.cap_for(lane, deps.config)
    for level in due:
        log.warning(
            "budget warning",
            extra={"lane": lane, "level": level, "spent_eur": round(spent, 4)},
        )
        await _alert_quietly(
            deps,
            "budget_warning",
            f"The {lane} lane has spent EUR {spent:.2f} of its EUR {cap:.2f} cap this month "
            f"({spent / cap:.0%}). New checks stop near the cap (RB-09).",
            f"{lane}_{level}",
        )


@router.post(
    "/llm/structured",
    tags=["llm"],
    responses={
        402: {"description": "Monthly budget cap reached"},
        409: {"description": "A call with this Idempotency-Key is still running"},
        429: {"description": "The account's daily quota of checks is used"},
    },
)
async def llm_structured(
    body: StructuredRequest, request: Request, idempotency_key: IdempotencyKey = None
) -> StructuredReply:
    """One schema-constrained LLM call, priced, capped, retried on transient errors and recorded."""
    deps = _deps(request)
    llm_request = LlmRequest(
        model=body.model,
        max_tokens=body.max_tokens,
        effort=body.effort,
        thinking=body.thinking,
        instructions=body.instructions,
        data=body.data,
        schema=body.output_schema,
    )
    try:
        result, call_id, cost = await structured_call(
            pool=deps.pool,
            provider=deps.provider,
            config=deps.config,
            request=llm_request,
            lane=body.lane,
            stage=body.stage,
            job_id=body.job_id,
            user_id=body.user_id,
            idempotency_key=idempotency_key,
        )
    except CallInProgress:
        raise _error(
            "call_in_progress", "The same call is still running for this job."
        ) from None
    except budget.QuotaReached as exc:
        log.info(
            "daily quota reached", extra={"user_id": exc.user_id, "quota": exc.quota}
        )
        raise _error("daily_quota_reached", QUOTA_MESSAGE) from None
    except budget.UnknownModel:
        raise _error(
            "unknown_model", f"{body.model} is not in the price table"
        ) from None
    except budget.CapReached as exc:
        log.warning(
            "budget cap reached",
            extra={
                "lane": exc.lane,
                "spent_eur": round(exc.spent, 4),
                "cap_eur": exc.cap,
            },
        )
        await _alert_quietly(
            deps,
            "budget_cap_reached",
            f"The {exc.lane} lane reached its cap: EUR {exc.spent:.2f} of {exc.cap:.2f} spent this month. "
            "New LLM calls are refused (RB-09).",
            exc.lane,
        )
        raise _error(
            "budget_cap_reached", "The monthly budget for this lane is used up."
        ) from None
    except LlmFailure as exc:
        raise _error(exc.code, exc.message) from None
    if not result.reused:
        await _warn(deps, body.lane)
    return StructuredReply(
        output=result.output,
        model=result.model,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_eur=round(cost, 6),
        call_id=call_id,
        reused=result.reused,
    )


async def _alert_quietly(deps: Deps, event: str, text: str, code: str | None) -> None:
    """An alert raised inside the gateway: a failure to send is logged, never passed to the caller."""
    if deps.alerts is None:
        return
    try:
        await deps.alerts.send(event, text, code)
    except AlertsNotConfigured:
        log.warning("alert not sent: alerts are not configured", extra={"event": event})
    except Exception:
        log.exception("alert not sent", extra={"event": event})


@router.post(
    "/notify",
    tags=["alerts"],
    responses={
        502: {"description": "Telegram refused or failed"},
        503: {"description": "Alerts are not configured"},
    },
)
async def notify(body: NotifyRequest, request: Request) -> NotifyReply:
    """Sends an alert to the owner's Telegram, unless it is a quiet event or a recent repeat."""
    alerts = _deps(request).alerts
    try:
        if alerts is None:
            raise AlertsNotConfigured("alerts are not set up")
        outcome = await alerts.send(body.event, body.text, body.code)
    except AlertsNotConfigured as exc:
        raise HTTPException(
            503, {"code": "alerts_not_configured", "message": str(exc)}
        ) from None
    except (AlertFailed, Transient) as exc:
        log.warning(
            "alert failed", extra={"event": body.event, "error": type(exc).__name__}
        )
        raise HTTPException(
            502,
            {"code": "alert_failed", "message": "Telegram did not accept the message."},
        ) from None
    return NotifyReply(outcome=outcome)


@router.post("/literature/search", tags=["literature"])
async def literature_search(body: SearchRequest, request: Request) -> dict:
    """PubMed search with the open-access filter; PMIDs in relevance order."""
    try:
        return await _deps(request).literature.search(body.query, body.max_results)
    except LiteratureUnavailable as exc:
        raise HTTPException(
            503, {"code": "literature_unavailable", "message": str(exc)}
        ) from None


@router.post("/literature/spell", tags=["literature"])
async def literature_spell(body: SpellRequest, request: Request) -> SpellReply:
    """PubMed's spelling suggestion for a supplement name; `suggestion` is null when there is none."""
    try:
        return SpellReply(**await _deps(request).literature.spell(body.term))
    except LiteratureUnavailable as exc:
        raise HTTPException(
            503, {"code": "literature_unavailable", "message": str(exc)}
        ) from None


@router.post("/literature/fetch", tags=["literature"])
async def literature_fetch(body: FetchRequest, request: Request) -> dict:
    """Licence and metadata for each PMID; JATS full text for the first allowed ones. Skips are counted."""
    try:
        result = await _deps(request).literature.fetch(body.pmids, body.full_texts)
    except LiteratureUnavailable as exc:
        raise HTTPException(
            503, {"code": "literature_unavailable", "message": str(exc)}
        ) from None
    return {
        "articles": result.articles,
        "full_texts": result.full_texts,
        "counts": result.counts,
    }


def _optional_secret(variable: str) -> str | None:
    path = os.environ.get(variable)
    if not path or not os.path.exists(path):
        return None
    return read_secret(path) or None


def build_app(
    checks: dict[str, Check] | None = None, deps: Deps | None = None
) -> FastAPI:
    if checks is None:
        checks = {"database": db_check(conninfo_from_env())}
    if deps is None and "SCC_DB_USER" in os.environ:
        conninfo = conninfo_from_env()
        # The active version in the database (D-048); a new one applies on restart.
        cfg = config.load_active(conninfo)
        pool = AsyncConnectionPool(conninfo, open=False, min_size=1, max_size=5)
        api_key = _optional_secret("SCC_ANTHROPIC_API_KEY_FILE")
        provider = (
            ClaudeProvider(api_key, cfg["llm"]["timeout_s"])
            if api_key
            else MissingProvider()
        )
        if not api_key:
            log.warning(
                "no LLM API key configured: LLM calls will fail with provider_not_configured"
            )
        deps = Deps(
            pool,
            provider,
            Literature(cfg, _optional_secret("SCC_NCBI_API_KEY_FILE"), pool),
            cfg,
            Alerts(
                cfg,
                _optional_secret("SCC_TELEGRAM_BOT_TOKEN_FILE"),
                _optional_secret("SCC_TELEGRAM_CHAT_ID_FILE"),
            ),
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

    app = create_app("gateway", "SuppClaimCheck gateway", checks, lifespan=lifespan)
    app.state.deps = deps
    app.include_router(router)
    # Read-only admin views for web's admin pages (P3.08-P3.10).
    app.include_router(admin_api.router)
    app.include_router(config_admin.router(config.STORE, admin_api.pool_of))
    return app
