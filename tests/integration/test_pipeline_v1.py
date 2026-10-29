"""P2.04/P2.05 acceptance: pipeline v1 end to end against a real database, with a fake gateway.

The fake answers core's gateway calls the way the real gateway does, so the test can check what core
asks for and in what order: metadata for every candidate first, then full texts for the best N by
evidence tier, then one assess call per article with the reason written before the label.
"""

import json

import httpx
from psycopg_pool import AsyncConnectionPool

from scc_core import config as core_config
from scc_core import jobs
from scc_core.gateway_client import GatewayClient
from scc_core.models import Claim, JobCreate
from scc_core.pipeline import run
from tests.helpers import run_async

FINDING = {
    "11": "Creatine supplementation increased leg press strength more than placebo in adults over 65.",
    "22": "Pooled across trials, creatine with resistance training improved strength in older adults.",
    "33": "In this narrative review we found no clear benefit of creatine for strength in older women.",
}
ARTICLES = {
    "11": {
        "pub_types": ["Randomized Controlled Trial"],
        "mesh": ["Humans"],
        "license": "cc by",
    },
    "22": {"pub_types": ["Meta-Analysis", "Review"], "mesh": [], "license": "cc by"},
    "33": {"pub_types": ["Review"], "mesh": ["Humans"], "license": "cc by"},
    "44": {
        "pub_types": ["Meta-Analysis"],
        "mesh": [],
        "license": "all rights reserved",
    },
}


def article(pmid: str) -> dict:
    meta = {
        "pmid": pmid,
        "pmcid": f"PMC{pmid}",
        "title": f"Study {pmid}",
        **ARTICLES[pmid],
    }
    if meta["license"] != "cc by":
        meta["skip_reason"] = "licence_not_allowed"
    return meta


def jats(pmid: str) -> str:
    return (
        "<article><body><sec><title>Results</title>"
        f"<p>{FINDING[pmid]}</p><p>Participants were recruited from two clinics.</p>"
        "</sec></body></article>"
    )


def test_pipeline_v1_ranks_by_tier_and_scores_by_article(migrated_db):
    calls: list[tuple[str, dict]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/literature/search":
            return httpx.Response(
                200, json={"count": 4, "pmids": ["33", "11", "44", "22"]}
            )
        if request.url.path == "/literature/fetch":
            articles = [article(p) for p in body["pmids"]]
            readable = [a for a in articles if "skip_reason" not in a][
                : body["full_texts"]
            ]
            return httpx.Response(
                200,
                json={
                    "articles": articles,
                    "full_texts": {a["pmcid"]: jats(a["pmid"]) for a in readable},
                    "counts": {
                        "licence_not_allowed": sum("skip_reason" in a for a in articles)
                    },
                },
            )
        if request.url.path == "/llm/structured":
            passages = json.loads(body["data"])["passages"]
            out = []
            for p in passages:
                pmid = p["passage_id"].split("-")[0].removeprefix("PMC")
                # The splitter merges short paragraphs, so the finding is inside a longer passage.
                relevant = FINDING[pmid] in p["text"]
                out.append(
                    {
                        "passage_id": p["passage_id"],
                        "reason": "A finding about strength." if relevant else "",
                        "label": ("contradicts" if pmid == "33" else "supports")
                        if relevant
                        else "not_relevant",
                        "quote": FINDING[pmid] if relevant else "",
                    }
                )
            return httpx.Response(
                200,
                json={"output": {"assessments": out}, "model": "m", "input_tokens": 100,
                      "output_tokens": 50, "cost_eur": 0.0001, "call_id": len(calls)},
            )  # fmt: skip
        return httpx.Response(404)

    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await pool.open()
        try:
            cfg = core_config.load("pipeline-v1")
            rt = run.Runtime(
                pool,
                GatewayClient("http://gateway", transport=httpx.MockTransport(gateway)),
                cfg,
            )
            async with pool.connection() as conn:
                job, _ = await jobs.create(
                    conn, JobCreate(user_id=1, input_text="Creatine makes older adults stronger."),
                    "v1-key", cfg["version"], None,
                )  # fmt: skip
                claim = Claim(
                    supplement="creatine",
                    effect="increases strength",
                    population="older adults",
                )
                await conn.execute(
                    "UPDATE core.jobs SET state = 'awaiting_confirmation' WHERE id = %s",
                    (job.id,),
                )
                await jobs.confirm(conn, job.id, claim)
            await run.run(rt, job.id)
            async with pool.connection() as conn:
                return await jobs.get(conn, job.id)
        finally:
            await pool.close()

    job = run_async(scenario())
    assert job.state == "done", job.error
    result = job.result

    fetches = [body for path, body in calls if path == "/literature/fetch"]
    # Metadata for all four candidates first, then full texts for the readable ones, strongest first.
    assert fetches[0] == {"pmids": ["33", "11", "44", "22"], "full_texts": 0}
    assert fetches[1] == {"pmids": ["22", "11", "33"], "full_texts": 5}
    assert [(a["pmid"], a["tier"], a["weight"]) for a in result["articles"]] == [
        ("22", "systematic_review", 1.0),
        ("11", "rct", 0.8),
        ("33", "narrative_review", 0.3),
    ]
    assert [(a["pmid"], a["tier"]) for a in result["found_not_assessed"]] == [
        ("44", "systematic_review")
    ]

    # One assess call per article, each asking for the reason before the label.
    assess = [body for path, body in calls if path == "/llm/structured"]
    assert len(assess) == 3
    for body in assess:
        item = body["schema"]["properties"]["assessments"]["items"]
        assert list(item["properties"]) == ["passage_id", "reason", "label", "quote"]

    # Rubric v1: for 1.0 + 0.8, against 0.3 (12.5% of the evidence, under the Mixed share).
    assert (result["verdict"], result["rubric_version"]) == ("Supported", "rubric-v1")
    assert (result["weight_for"], result["weight_against"], result["strength"]) == (
        1.8,
        0.3,
        1.0,
    )
    assert (
        result["score"] == 86
    )  # direction (1.8 - 0.3) / 2.1 = 0.714; 50 + 50 x 0.714 x 1.0 = 85.7
    assert (
        result["supporting"],
        result["contradicting"],
        result["quotes_dropped"],
    ) == (2, 1, 0)
    assert result["searched"]["ranking"] == "tier"
    assert "strongest study types first" in result["limitations"][1]
