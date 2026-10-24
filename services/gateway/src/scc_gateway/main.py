"""The gateway: the only module that calls outside services (LLM, literature, alerts; D-011, D-033).

Internal only. Holds every API key, so the budget cap can't be bypassed (P1.06, P1.07).
"""

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from scc_common.app import Check, create_app
from scc_common.db import conninfo_from_env, db_check, read_secret
from scc_gateway import budget, config
from scc_gateway.literature import Literature, LiteratureUnavailable
from scc_gateway.llm import (
    ClaudeProvider,
    LlmFailure,
    LlmRequest,
    MissingProvider,
    Provider,
    structured_call,
)

log = logging.getLogger("scc.gateway")


@dataclass
class Deps:
    pool: AsyncConnectionPool
    provider: Provider
    literature: Literature
    config: dict


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


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    max_results: Annotated[int, Field(ge=1, le=100)] = 20


class FetchRequest(BaseModel):
    pmids: list[str] = Field(max_length=100)
    full_texts: Annotated[int, Field(ge=0, le=10)] = 1


# HTTP status for each LLM failure code. 402 for the budget, so callers can tell it apart.
_STATUS = {
    "budget_cap_reached": 402,
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


@router.post(
    "/llm/structured",
    tags=["llm"],
    responses={402: {"description": "Monthly budget cap reached"}},
)
async def llm_structured(body: StructuredRequest, request: Request) -> StructuredReply:
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
        )
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
        raise _error(
            "budget_cap_reached", "The monthly budget for this lane is used up."
        ) from None
    except LlmFailure as exc:
        raise _error(exc.code, exc.message) from None
    return StructuredReply(
        output=result.output,
        model=result.model,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_eur=round(cost, 6),
        call_id=call_id,
    )


@router.post("/literature/search", tags=["literature"])
async def literature_search(body: SearchRequest, request: Request) -> dict:
    """PubMed search with the open-access filter; PMIDs in relevance order."""
    try:
        return await _deps(request).literature.search(body.query, body.max_results)
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
        cfg = config.load()
        pool = AsyncConnectionPool(
            conninfo_from_env(), open=False, min_size=1, max_size=5
        )
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
    return app
