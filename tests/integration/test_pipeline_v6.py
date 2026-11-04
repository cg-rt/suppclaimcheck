"""Query v2 in the pipeline (D-052): a candidate given by injection or infusion is found, not read.

The fake gateway from test_pipeline_v1 shapes, on pipeline-v1 with pipeline-v6's [query] section, so
only the search stage differs: the marked candidate is never asked for in full text, it is listed as
found but not read with its reason, and the search's skipped counts name it.
"""

import json

import httpx
from psycopg_pool import AsyncConnectionPool

from scc_core import config as core_config
from scc_core import jobs
from scc_core.gateway_client import GatewayClient
from scc_core.models import Claim, JobCreate
from scc_core.pipeline import query, run
from tests.helpers import run_async

FINDING = (
    "Daily oral iron raised haemoglobin and reduced anaemia in menstruating women."
)
TITLES = {
    "11": "Daily iron supplementation in menstruating women: a randomised trial",
    "22": "Intravenous iron sucrose versus oral iron for anaemia in women",
}


def test_a_candidate_given_by_infusion_is_found_not_read(migrated_db):
    calls: list[tuple[str, dict]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if request.url.path == "/literature/search":
            return httpx.Response(200, json={"count": 2, "pmids": ["22", "11"]})
        if request.url.path == "/literature/fetch":
            articles = [
                {"pmid": p, "pmcid": f"PMC{p}", "title": TITLES[p], "license": "cc by",
                 "pub_types": ["Randomized Controlled Trial"], "mesh": ["Humans"]}
                for p in body["pmids"]
            ]  # fmt: skip
            texts = {
                a[
                    "pmcid"
                ]: f"<article><body><sec><title>Results</title><p>{FINDING}</p></sec></body></article>"
                for a in articles[: body["full_texts"]]
            }
            return httpx.Response(
                200, json={"articles": articles, "full_texts": texts, "counts": {}}
            )
        if request.url.path == "/llm/structured":
            passages = json.loads(body["data"])["passages"]
            out = [
                {"passage_id": p["passage_id"], "label": "supports", "quote": FINDING,
                 "reason": "Oral iron reduced anaemia."}
                for p in passages
            ]  # fmt: skip
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
            cfg = {
                **core_config.load("pipeline-v1"),
                "query": core_config.load("pipeline-v6")["query"],
            }
            rt = run.Runtime(
                pool,
                GatewayClient("http://gateway", transport=httpx.MockTransport(gateway)),
                cfg,
            )
            async with pool.connection() as conn:
                job, _ = await jobs.create(
                    conn, JobCreate(user_id=1, input_text="Iron supplements reduce anaemia."),
                    "v6-key", cfg["version"], None,
                )  # fmt: skip
                await conn.execute(
                    "UPDATE core.jobs SET state = 'awaiting_confirmation' WHERE id = %s",
                    (job.id,),
                )
                await jobs.confirm(
                    conn,
                    job.id,
                    Claim(
                        supplement="iron", effect="reduces anaemia", population="women"
                    ),
                )
            await run.run(rt, job.id)
            async with pool.connection() as conn:
                return await jobs.get(conn, job.id)
        finally:
            await pool.close()

    job = run_async(scenario())
    assert job.state == "done", job.error
    result = job.result
    search = next(body for path, body in calls if path == "/literature/search")
    assert "(anaemia[tiab] OR anemia[tiab])" in search["query"]
    fetches = [body for path, body in calls if path == "/literature/fetch"]
    assert fetches[0] == {"pmids": ["22", "11"], "full_texts": 0}
    assert fetches[1]["pmids"] == ["11"]  # the infusion trial is never read
    assert [a["pmid"] for a in result["articles"]] == ["11"]
    assert [(a["pmid"], a["skip_reason"]) for a in result["found_not_assessed"]] == [
        ("22", query.NOT_BY_MOUTH)
    ]
    assert result["searched"]["skipped"][query.NOT_BY_MOUTH] == 1
    assert result["searched"]["query_version"] == "query-v2"
