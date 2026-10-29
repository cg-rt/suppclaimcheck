"""The pipeline (P1.08, P2.04, P2.12): extract, then (after the user confirms) search, retrieve, assess, verify, score.

Each step records the job's state, so progress is visible and a failure says where it happened.
The LLM only extracts claims and labels passages; code builds the query, ranks passages, checks
every quote and computes the verdict (AGENTS.md sections 6 and 8).
"""

import asyncio
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
from scc_core.pipeline import embeddings, evidence, jats, query, rubric
from scc_core.pipeline.quotes import verify_quote

log = logging.getLogger("scc.pipeline")

# The most articles one check may read (P2.05): cost and latency grow with each one.
MAX_ARTICLES = 10

# Messages users see when a job fails; no stack traces or internal detail (AGENTS.md section 8).
MESSAGES = {
    "budget_cap_reached": "This month's budget for checks is used up. Checks will work again next month.",
    "provider_unavailable": "The language model service is not answering. Please try again later.",
    "provider_not_configured": "Checks are not available yet: the language model is not set up.",
    "literature_unavailable": "The literature databases are not answering. Please try again later.",
    "invalid_output": "The check could not be completed because the model's answer was not usable.",
    "no_claim": "No claim about a dietary supplement was found in the text.",
}

# Shown when PubMed matched nothing at all (P2.12): the search missed, which is not the same as
# research saying nothing.
NO_MATCHES = (
    "The search found no articles at all. That doesn't mean no research exists: check the spelling "
    "of the supplement name, or try another name for it, in a new check."
)


@dataclass
class Runtime:
    pool: AsyncConnectionPool
    gateway: GatewayClient
    config: dict
    # Hybrid retrieval's model (P2.06); loaded from the config's model name when not given (tests pass a fake).
    embedder: embeddings.Embedder | None = None

    def embedding_model(self) -> embeddings.Embedder:
        if self.embedder is None:
            self.embedder = embeddings.load(self.config["retrieval"]["embedding_model"])
        return self.embedder


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


def ordered(schema: dict, order: list[str]) -> dict:
    """The schema with each object that has exactly these fields listing them in this order.

    Structured output writes fields in schema order, so the order decides what the model commits to
    first: with "reason" before "label", it explains a passage before it labels it (P2.04, D-044).
    """
    if isinstance(schema, list):
        return [ordered(v, order) for v in schema]
    if not isinstance(schema, dict):
        return schema
    node = {k: ordered(v, order) for k, v in schema.items()}
    props = node.get("properties")
    if isinstance(props, dict) and set(props) == set(order):
        node["properties"] = {k: props[k] for k in order}
        node["required"] = list(order)
    return node


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
    schema = strict_schema(output)
    if settings.get("field_order"):
        schema = ordered(schema, settings["field_order"])
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
                schema=schema,
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
    claims = [c.model_dump() for c in extraction.claims]
    if rt.config.get("query", {}).get("spelling_check"):
        await _spelling(rt, job_id, claims)
    await _set(
        rt,
        job_id,
        JobState.awaiting_confirmation,
        f"{len(extraction.claims)} claim(s)",
        extracted_claims=claims,
    )


async def _spelling(rt: Runtime, job_id: UUID, claims: list[dict]) -> None:
    """Adds PubMed's spelling suggestion for each supplement name (P2.12), for the confirm page to offer.

    Only a suggestion: the user decides. A spelling check that fails leaves the claims as they are.
    """
    checked: dict[str, str | None] = {}
    for claim in claims:
        name = claim["supplement"].strip()
        if name not in checked:
            try:
                checked[name] = (await rt.gateway.spell(name))["suggestion"]
            except (GatewayError, httpx.HTTPError) as exc:
                log.warning(
                    "spelling check failed",
                    extra={"job_id": str(job_id), "error": type(exc).__name__},
                )
                checked[name] = None
        claim["spelling_suggestion"] = checked[name]


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
    search_query = query.build(claim, cfg.get("query"))
    found = await rt.gateway.search(search_query, cfg["search"]["pubmed_max_results"])
    searched = {
        "query": search_query,
        "query_version": query.version(cfg.get("query")),
        "database": "PubMed (open-access filter)",
        "matches": found["count"],
        "considered": len(found["pmids"]),
    }

    await _set(rt, job_id, JobState.retrieving, f"{len(found['pmids'])} PubMed IDs")
    n = cfg["search"]["articles_to_assess"]
    if not 1 <= n <= MAX_ARTICLES:
        raise ValueError(f"articles_to_assess must be 1-{MAX_ARTICLES}, not {n}")
    mode = cfg["search"].get("ranking", "relevance")
    weights = cfg.get("evidence", {}).get("weights")
    if not found["pmids"]:
        listed = fetched = {"articles": [], "full_texts": {}, "counts": {}}
    elif mode == "relevance":
        # Pipeline v0: full texts for the first N readable articles in PubMed's order.
        listed = fetched = await rt.gateway.fetch(found["pmids"], n)
    else:
        # Metadata for every candidate first (the gateway caches it), so code can rank by evidence tier;
        # then full texts for the best N, in that order, skipping any without full text.
        listed = await rt.gateway.fetch(found["pmids"], 0)
        order = [a["pmid"] for a in evidence.rank(listed["articles"], weights, mode)]
        fetched = await rt.gateway.fetch(order, n) if order else listed
    ranked_articles = evidence.rank(fetched["articles"], weights, mode)
    assessed_articles = [
        a for a in ranked_articles if a["pmcid"] in fetched["full_texts"]
    ]
    searched["skipped"] = {
        **listed["counts"],
        "no_full_text": fetched["counts"].get("no_full_text", 0),
    }
    searched["ranking"] = mode
    if not assessed_articles:
        await _set(rt, job_id, JobState.scoring)
        result = _result(
            _score(cfg, [], []), searched, [], [], listed["articles"], 0, cfg
        )
        if found["count"] == 0 and "query" in cfg:
            result["limitations"].insert(0, NO_MATCHES)
        return result

    searched["retrieval"] = cfg["retrieval"].get("mode", "keyword")
    query_vector = None
    if searched["retrieval"] == "hybrid":
        model = rt.embedding_model()
        query_vector = await asyncio.to_thread(model.query, _claim_text(claim))
    passages: dict[str, list[dict]] = {}
    for article in assessed_articles:
        passages[article["pmcid"]] = await _rank_passages(
            rt,
            job_id,
            claim,
            article,
            fetched["full_texts"][article["pmcid"]],
            query_vector,
        )
    total = sum(len(p) for p in passages.values())
    await _set(
        rt,
        job_id,
        JobState.assessing,
        f"{total} passages in {len(passages)} article(s)",
    )
    assessments, unusable = await _assess(rt, job_id, user_id, claim, passages)
    assessed_articles = [a for a in assessed_articles if a["pmcid"] not in unusable]

    await _set(rt, job_id, JobState.verifying)
    by_id = {p["passage_id"]: p for ps in passages.values() for p in ps}
    quotes, dropped = await _verify(rt, job_id, assessments, by_id)

    await _set(rt, job_id, JobState.scoring)
    result = _result(
        _score(cfg, assessed_articles, quotes),
        searched,
        assessed_articles,
        quotes,
        listed["articles"],
        dropped,
        cfg,
    )
    if unusable:
        result["limitations"].insert(
            0,
            f"{len(unusable)} article(s) could not be assessed: the model's answer was not usable.",
        )
    return result


async def _assess(
    rt: Runtime,
    job_id: UUID,
    user_id: int,
    claim: Claim,
    passages: dict[str, list[dict]],
) -> tuple[Assessment, set[str]]:
    """One assess call per article, a few at a time. An article whose answer stays unusable is left out;
    any other failure (budget, provider down) fails the job."""
    limit = asyncio.Semaphore(rt.config["llm"]["assess"].get("concurrency", 1))

    async def one(items: list[dict]) -> Assessment:
        data = json.dumps(
            {
                "claim": claim.model_dump(),
                "passages": [
                    {
                        "passage_id": p["passage_id"],
                        "section": p["section"],
                        "text": p["text"],
                    }
                    for p in items
                ],
            },
            ensure_ascii=False,
            indent=1,
        )
        async with limit:
            return await _structured(rt, "assess", job_id, user_id, data, Assessment)

    pmcids = list(passages)
    outcomes = await asyncio.gather(
        *(one(passages[p]) for p in pmcids), return_exceptions=True
    )
    merged: list = []
    unusable: set[str] = set()
    for pmcid, outcome in zip(pmcids, outcomes, strict=True):
        if isinstance(outcome, StageFailed) and outcome.code == "invalid_output":
            unusable.add(pmcid)
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            merged += outcome.assessments
    if len(unusable) == len(pmcids):
        raise StageFailed("invalid_output")
    return Assessment(assessments=merged), unusable


def _score(cfg: dict, assessed: list[dict], quotes: list[dict]):
    """Counts each article's verified quotes and hands them to the configured rubric."""
    if cfg["rubric"]["version"] == "rubric-v0":
        supporting = sum(1 for q in quotes if q["label"] == "supports")
        contradicting = sum(1 for q in quotes if q["label"] == "contradicts")
        return rubric.score(len(assessed), supporting, contradicting, cfg["rubric"])
    return rubric.score_v1(
        [
            rubric.ArticleEvidence(
                pmcid=a["pmcid"],
                tier=a["tier"],
                weight=a["weight"],
                supporting=sum(
                    1
                    for q in quotes
                    if q["pmcid"] == a["pmcid"] and q["label"] == "supports"
                ),
                contradicting=sum(
                    1
                    for q in quotes
                    if q["pmcid"] == a["pmcid"] and q["label"] == "contradicts"
                ),
            )
            for a in assessed
        ],
        {**cfg["rubric"], "human_tiers": cfg["evidence"]["human_tiers"]},
    )


def _claim_text(claim: Claim) -> str:
    """The claim as one sentence, for the query embedding."""
    parts = [claim.supplement, claim.effect]
    if claim.population:
        parts.append(f"in {claim.population}")
    return " ".join(parts)


async def _rank_passages(
    rt: Runtime,
    job_id: UUID,
    claim: Claim,
    article: dict,
    xml: str,
    query_vector: list[float] | None = None,
) -> list[dict]:
    """Stores the article's passages and returns the top K.

    Keyword mode (v0-v2): by Postgres full-text rank. Hybrid mode (P2.06): the full-text order and the
    order by cosine distance to the claim's embedding, fused with weighted reciprocal rank fusion.
    """
    cfg = rt.config["retrieval"]
    pieces = jats.passages(xml, cfg["passage_max_chars"], cfg["skip_sections"])
    words = query.text_query(claim, rt.config.get("query"))
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
            """,
            (job_id, article["pmcid"], words),
        )
        rows = await cur.fetchall()
        if query_vector is not None:
            model = rt.embedding_model()
            embedded = await embeddings.ensure(conn, model, [t for _, _, t in rows])
            # Distances are rounded so tiny float differences between machines don't reorder passages.
            await cur.execute(
                """
                SELECT p.ordinal FROM core.passages p
                JOIN core.embeddings e
                  ON e.model = %s
                 AND e.text_hash = encode(sha256(convert_to(p.text, 'UTF8')), 'hex')
                WHERE p.job_id = %s AND p.pmcid = %s
                ORDER BY round((e.embedding <=> %s::vector)::numeric, 4), p.ordinal
                """,
                (
                    model.name,
                    job_id,
                    article["pmcid"],
                    embeddings.literal(query_vector),
                ),
            )
            by_vector = [row[0] for row in await cur.fetchall()]
            order = embeddings.fuse(
                [o for o, _, _ in rows], by_vector, cfg["keyword_weight"], cfg["rrf_k"]
            )
            by_ordinal = {row[0]: row for row in rows}
            rows = [by_ordinal[o] for o in order]
            log.info(
                "passages ranked",
                extra={
                    "job_id": str(job_id),
                    "pmcid": article["pmcid"],
                    "passages": len(rows),
                    "embedded": embedded,
                },
            )
        rows = rows[: cfg["top_k"]]
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
            {**a, "tier": evidence.tier(a)}
            for a in considered
            if a.get("pmcid") not in assessed_ids
        ],
        "quotes": quotes,
        "quotes_dropped": dropped,
        "searched": searched,
        "limitations": [
            "Only open-access articles whose licence allows text mining are read.",
            f"This version reads up to {cfg['search']['articles_to_assess']} article(s) per claim"
            + (
                ", choosing the strongest study types first."
                if searched.get("ranking") == "tier"
                else "."
            ),
            "This is not medical advice.",
        ],
    }
