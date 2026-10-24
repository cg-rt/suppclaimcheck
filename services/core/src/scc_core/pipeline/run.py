"""Pipeline v0 (P1.08): extract, then (after the user confirms) search, retrieve, assess, verify, score.

Each step records the job's state, so progress is visible and a failure says where it happened.
The LLM only extracts claims and labels passages; code builds the query, ranks passages, checks
every quote and computes the verdict (AGENTS.md sections 6 and 8).
"""

import json
import logging
from dataclasses import dataclass
from uuid import UUID

import httpx
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ValidationError

from scc_core import jobs
from scc_core.gateway_client import GatewayClient, GatewayError
from scc_core.models import Assessment, Claim, Extraction, JobState
from scc_core.pipeline import jats, query, rubric
from scc_core.pipeline.quotes import verify_quote

log = logging.getLogger("scc.pipeline")

# Messages users see when a job fails; no stack traces or internal detail (AGENTS.md section 8).
MESSAGES = {
    "budget_cap_reached": "This month's budget for checks is used up. Checks will work again next month.",
    "provider_unavailable": "The language model service is not answering. Please try again later.",
    "provider_not_configured": "Checks are not available yet: the language model is not set up.",
    "literature_unavailable": "The literature databases are not answering. Please try again later.",
    "invalid_output": "The check could not be completed because the model's answer was not usable.",
    "no_claim": "No claim about a dietary supplement was found in the text.",
}


@dataclass
class Runtime:
    pool: AsyncConnectionPool
    gateway: GatewayClient
    config: dict


class StageFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def strict_schema(model: type[BaseModel]) -> dict:
    """The model's JSON schema with every $ref inlined and titles dropped, for structured-output mode."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(definitions[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


async def _structured(
    rt: Runtime,
    stage: str,
    job_id: UUID,
    user_id: int,
    data: str,
    output: type[BaseModel],
):
    """One LLM call with schema-checked output; an unusable answer is retried once, then the stage fails."""
    settings = rt.config["llm"][stage]
    for attempt in (1, 2):
        try:
            reply = await rt.gateway.structured(
                stage=stage,
                job_id=job_id,
                user_id=user_id,
                settings=settings,
                model=rt.config["llm"]["model"],
                instructions=rt.config["prompts"][stage],
                data=data,
                schema=strict_schema(output),
            )
            return output.model_validate(reply["output"])
        except ValidationError as exc:
            log.warning(
                "structured output failed validation",
                extra={"stage": stage, "attempt": attempt, "error": str(exc)[:500]},
            )
        except GatewayError as exc:
            if exc.code not in {"output_not_json", "output_truncated"}:
                raise StageFailed(
                    exc.code if exc.code in MESSAGES else "provider_unavailable"
                ) from exc
            log.warning(
                "structured output unusable",
                extra={"stage": stage, "attempt": attempt, "code": exc.code},
            )
    raise StageFailed("invalid_output")


async def _set(
    rt: Runtime, job_id: UUID, state: JobState, detail: str | None = None, **fields
) -> None:
    async with rt.pool.connection() as conn:
        await jobs.set_state(conn, job_id, state, detail, **fields)


async def _fail(rt: Runtime, job_id: UUID, code: str) -> None:
    log.warning("job failed", extra={"job_id": str(job_id), "code": code})
    await _set(
        rt,
        job_id,
        JobState.failed,
        code,
        error=MESSAGES.get(code, MESSAGES["provider_unavailable"]),
    )
    # The gateway decides whether this code is worth a message (P1.14); an alert never fails the job twice.
    try:
        await rt.gateway.notify("job_failed", f"Job {job_id} stopped.", code)
    except (GatewayError, httpx.HTTPError) as exc:
        log.warning(
            "failure alert not sent",
            extra={"job_id": str(job_id), "error": type(exc).__name__},
        )


async def extract(rt: Runtime, job_id: UUID) -> None:
    async with rt.pool.connection() as conn:
        job = await jobs.get(conn, job_id)
    if job is None or job.state != JobState.submitted:
        log.info(
            "extract skipped",
            extra={"job_id": str(job_id), "state": job.state if job else None},
        )
        return
    await _set(rt, job_id, JobState.extracting)
    try:
        extraction = await _structured(
            rt, "extract", job_id, job.user_id, job.input_text, Extraction
        )
    except StageFailed as exc:
        await _fail(rt, job_id, exc.code)
        return
    if not extraction.claims:
        await _fail(rt, job_id, "no_claim")
        return
    await _set(
        rt,
        job_id,
        JobState.awaiting_confirmation,
        f"{len(extraction.claims)} claim(s)",
        extracted_claims=[c.model_dump() for c in extraction.claims],
    )


async def run(rt: Runtime, job_id: UUID) -> None:
    async with rt.pool.connection() as conn:
        job = await jobs.get(conn, job_id)
    if job is None or job.state != JobState.searching or job.confirmed_claim is None:
        log.info(
            "pipeline skipped",
            extra={"job_id": str(job_id), "state": job.state if job else None},
        )
        return
    try:
        result = await _run(rt, job_id, job.user_id, job.confirmed_claim)
    except StageFailed as exc:
        await _fail(rt, job_id, exc.code)
        return
    except GatewayError as exc:
        await _fail(
            rt, job_id, exc.code if exc.code in MESSAGES else "literature_unavailable"
        )
        return
    await _set(rt, job_id, JobState.done, result["verdict"], result=result)


async def _run(rt: Runtime, job_id: UUID, user_id: int, claim: Claim) -> dict:
    cfg = rt.config
    search_query = query.build(claim)
    found = await rt.gateway.search(search_query, cfg["search"]["pubmed_max_results"])
    searched = {
        "query": search_query,
        "query_version": query.VERSION,
        "database": "PubMed (open-access filter)",
        "matches": found["count"],
        "considered": len(found["pmids"]),
    }

    await _set(rt, job_id, JobState.retrieving, f"{len(found['pmids'])} PubMed IDs")
    fetched = await rt.gateway.fetch(found["pmids"], cfg["search"]["articles_to_assess"]) if found["pmids"] else {
        "articles": [], "full_texts": {}, "counts": {}
    }  # fmt: skip
    searched["skipped"] = fetched["counts"]
    assessed_articles = [
        a for a in fetched["articles"] if a["pmcid"] in fetched["full_texts"]
    ]
    if not assessed_articles:
        await _set(rt, job_id, JobState.scoring)
        return _result(
            rubric.score(0, 0, 0, cfg["rubric"]),
            searched,
            [],
            [],
            fetched["articles"],
            0,
            cfg,
        )

    ranked: list[dict] = []
    for article in assessed_articles:
        ranked += await _rank_passages(
            rt, job_id, claim, article, fetched["full_texts"][article["pmcid"]]
        )

    await _set(rt, job_id, JobState.assessing, f"{len(ranked)} passages")
    by_id = {p["passage_id"]: p for p in ranked}
    data = json.dumps(
        {
            "claim": claim.model_dump(),
            "passages": [
                {
                    "passage_id": p["passage_id"],
                    "section": p["section"],
                    "text": p["text"],
                }
                for p in ranked
            ],
        },
        ensure_ascii=False,
        indent=1,
    )
    assessment = await _structured(rt, "assess", job_id, user_id, data, Assessment)

    await _set(rt, job_id, JobState.verifying)
    quotes, dropped = await _verify(rt, job_id, assessment, by_id)

    await _set(rt, job_id, JobState.scoring)
    supporting = sum(1 for q in quotes if q["label"] == "supports")
    contradicting = sum(1 for q in quotes if q["label"] == "contradicts")
    score = rubric.score(
        len(assessed_articles), supporting, contradicting, cfg["rubric"]
    )
    return _result(
        score, searched, assessed_articles, quotes, fetched["articles"], dropped, cfg
    )


async def _rank_passages(
    rt: Runtime, job_id: UUID, claim: Claim, article: dict, xml: str
) -> list[dict]:
    """Stores the article's passages and returns the top K by Postgres full-text rank (keyword ranking v0)."""
    cfg = rt.config["retrieval"]
    pieces = jats.passages(xml, cfg["passage_max_chars"], cfg["skip_sections"])
    async with rt.pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO core.passages (job_id, pmcid, ordinal, section, text) VALUES (%s, %s, %s, %s, %s)",
            [(job_id, article["pmcid"], p.ordinal, p.section, p.text) for p in pieces],
        )
        await cur.execute(
            """
            SELECT ordinal, section, text FROM core.passages
            WHERE job_id = %s AND pmcid = %s
            ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('english', %s)) DESC, ordinal
            LIMIT %s
            """,
            (job_id, article["pmcid"], query.text_query(claim), cfg["top_k"]),
        )
        rows = await cur.fetchall()
    return [
        {
            "passage_id": f"{article['pmcid']}-{ordinal}",
            "pmcid": article["pmcid"],
            "section": section,
            "text": text,
        }
        for ordinal, section, text in rows
    ]


async def _verify(
    rt: Runtime, job_id: UUID, assessment: Assessment, by_id: dict
) -> tuple[list[dict], int]:
    """Keeps only quotes found word for word in the cited passage; logs every other one as a hallucination event."""
    min_chars = rt.config["quotes"]["min_chars"]
    kept: list[dict] = []
    events: list[tuple] = []
    seen: set[str] = set()
    for item in assessment.assessments:
        if item.label == "not_relevant" or item.passage_id in seen:
            continue
        seen.add(item.passage_id)
        passage = by_id.get(item.passage_id)
        if passage is None:
            events.append(
                (
                    job_id,
                    "assess",
                    item.passage_id,
                    item.quote,
                    "unknown_passage",
                    rt.config["version"],
                )
            )
            continue
        check = verify_quote(item.quote, passage["text"], min_chars)
        if not check.verified:
            events.append(
                (
                    job_id,
                    "assess",
                    item.passage_id,
                    item.quote,
                    check.reason,
                    rt.config["version"],
                )
            )
            continue
        kept.append(
            {
                "passage_id": item.passage_id,
                "pmcid": passage["pmcid"],
                "section": passage["section"],
                "label": item.label,
                "quote": item.quote,
                "reason": item.reason,
            }
        )
    if events:
        log.warning(
            "quotes dropped", extra={"job_id": str(job_id), "dropped": len(events)}
        )
        async with rt.pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO core.hallucination_events (job_id, stage, passage_ref, quote, reason, config_version)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                events,
            )
    return kept, len(events)


def _result(score, searched, assessed, quotes, considered, dropped, cfg) -> dict:
    assessed_ids = {a["pmcid"] for a in assessed}
    return {
        **score.as_dict(),
        "config_version": cfg["version"],
        "articles": assessed,
        "found_not_assessed": [
            a for a in considered if a.get("pmcid") not in assessed_ids
        ],
        "quotes": quotes,
        "quotes_dropped": dropped,
        "searched": searched,
        "limitations": [
            "Only open-access articles whose licence allows text mining are read.",
            f"This version reads up to {cfg['search']['articles_to_assess']} article(s) per claim.",
            "This is not medical advice.",
        ],
    }
