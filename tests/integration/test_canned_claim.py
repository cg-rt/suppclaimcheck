"""P1.08 acceptance: a canned claim runs end to end against recorded fixtures, with no live calls.

Real code everywhere except the outside world: core-api, the pipeline tasks, the gateway app,
the Anthropic SDK and the literature client all run as in production, against a real database.
Only the HTTP transports to Anthropic, PubMed and Europe PMC are replaced by fixtures
(tests/fixtures/canned_claim/README.md). Procrastinate uses its in-memory connector; the test
plays the worker by running each queued task itself.
"""

import json
from pathlib import Path
from uuid import UUID

import anthropic
import httpx
import httpx2
import psycopg
from procrastinate import App
from procrastinate.testing import InMemoryConnector
from psycopg_pool import AsyncConnectionPool

from scc_common.recorded import ReplayTransport
from scc_core import api as core_api
from scc_core import config as core_config
from scc_core import tasks
from scc_core.gateway_client import GatewayClient
from scc_core.pipeline.run import Runtime
from scc_gateway import config as gateway_config
from scc_gateway import main as gateway_main
from scc_gateway.literature import Literature
from scc_gateway.llm import DATA_CLOSE, DATA_OPEN, ClaudeProvider
from tests.helpers import run_async

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "canned_claim"
SUBMITTED = "Taking creatine while doing resistance training makes older adults stronger. Ask your coach about it!"
CORRELATION_ID = "canned-claim-0001"
# The P1.08 acceptance test pins pipeline v0: its fixtures hold one article, which v0 reads.
V0 = "pipeline-v0"


def anthropic_fixture_client(seen: list[dict]):
    """The real Anthropic SDK, with its HTTP transport answering from the fixture files."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        stage = (
            "extract"
            if "claims" in body["output_config"]["format"]["schema"]["properties"]
            else "assess"
        )
        return httpx2.Response(
            200,
            json=json.loads((FIXTURES / "llm" / f"{stage}.json").read_text("utf-8")),
        )

    return anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))


def test_canned_claim_end_to_end(migrated_db):
    llm_requests: list[dict] = []
    literature_transport = ReplayTransport(FIXTURES / "literature")

    async def scenario():
        gateway_pool = AsyncConnectionPool(migrated_db.conninfo("gateway"), open=False)
        core_pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await gateway_pool.open()
        await core_pool.open()
        try:
            gcfg = gateway_config.load()
            provider = ClaudeProvider(
                "test-key", 30, http_client=anthropic_fixture_client(llm_requests)
            )
            gateway_app = gateway_main.build_app(
                checks={},
                deps=gateway_main.Deps(
                    gateway_pool,
                    provider,
                    Literature(gcfg, None, gateway_pool, literature_transport),
                    gcfg,
                ),
            )
            gateway = GatewayClient(
                "http://gateway", transport=httpx.ASGITransport(app=gateway_app)
            )
            tasks.runtime = Runtime(core_pool, gateway, core_config.load(V0))

            connector = InMemoryConnector()
            queue = App(connector=connector)
            queue.add_tasks_from(tasks.blueprint, namespace="core")
            core_app = core_api.build_app(
                checks={},
                deps=core_api.Deps(core_pool, queue, core_config.load(V0)["version"]),
            )

            async with (
                queue.open_async(),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=core_app), base_url="http://core"
                ) as core,
            ):
                created = await core.post(
                    "/jobs",
                    json={"user_id": 1, "input_text": SUBMITTED},
                    headers={
                        "Idempotency-Key": "canned-claim-key-1",
                        "X-Correlation-ID": CORRELATION_ID,
                    },
                )
                assert created.status_code == 201, created.text
                job_id = created.json()["id"]

                # The worker's part: run the queued extraction with the job's correlation ID.
                (queued,) = connector.jobs.values()
                assert queued["task_name"] == tasks.EXTRACT
                assert queued["args"] == {
                    "job_id": job_id,
                    "correlation_id": CORRELATION_ID,
                }
                await tasks.extract_claim(**queued["args"])

                job = (await core.get(f"/jobs/{job_id}")).json()
                assert job["state"] == "awaiting_confirmation"
                assert job["extracted_claims"][0]["supplement"] == "creatine"
                # Nothing has been searched before the user confirms.
                assert literature_transport.requests == []

                claim = {
                    k: job["extracted_claims"][0][k]
                    for k in ("supplement", "effect", "population", "dose", "duration")
                }
                confirmed = await core.post(
                    f"/jobs/{job_id}/confirm",
                    json=claim,
                    headers={"X-Correlation-ID": CORRELATION_ID},
                )
                assert confirmed.status_code == 200, confirmed.text
                run_job = next(
                    j for j in connector.jobs.values() if j["task_name"] == tasks.RUN
                )
                await tasks.run_pipeline(**run_job["args"])

                return (await core.get(f"/jobs/{job_id}")).json()
        finally:
            await core_pool.close()
            await gateway_pool.close()

    job = run_async(scenario())
    result = job["result"]

    assert job["state"] == "done", job["error"]
    # Three verbatim quotes support the claim; the planted fabricated one is dropped.
    assert result["verdict"] == "Supported"
    assert (
        result["supporting"],
        result["contradicting"],
        result["quotes_dropped"],
    ) == (3, 0, 1)
    assert result["score"] == 75  # 50 + 50 x direction 1.0 x strength 3/6 (rubric v0)
    assert result["articles"][0]["pmcid"] == "PMC7871530"
    assert result["articles"][0]["license"] == "cc by"
    assert {q["passage_id"] for q in result["quotes"]} == {
        "PMC7871530-22",
        "PMC7871530-0",
        "PMC7871530-37",
    }
    assert result["searched"]["considered"] == 20
    assert len(result["found_not_assessed"]) == 19

    # Every outside call went through the gateway's recorded transports; nothing else was requested.
    assert len(literature_transport.requests) == 3
    assert len(llm_requests) == 2
    for body in llm_requests:
        assert "tools" not in body and "temperature" not in body
        content = body["messages"][0]["content"]
        assert content.startswith(DATA_OPEN) and content.endswith(DATA_CLOSE)
        assert "untrusted" in body["system"]

    with psycopg.connect(migrated_db.admin_conninfo) as conn:
        events = conn.execute(
            "SELECT passage_ref, reason FROM core.hallucination_events WHERE job_id = %s",
            (UUID(job["id"]),),
        ).fetchall()
        calls = conn.execute(
            "SELECT stage, status, model, input_tokens, output_tokens, cost_eur, correlation_id, user_id"
            " FROM gateway.llm_calls ORDER BY id"
        ).fetchall()
        states = [
            r[0]
            for r in conn.execute(
                "SELECT state FROM core.job_events WHERE job_id = %s ORDER BY id",
                (UUID(job["id"]),),
            )
        ]
    assert events == [("PMC7871530-31", "not_in_passage")]
    assert [(c[0], c[1], c[2]) for c in calls] == [
        ("extract", "ok", "claude-haiku-5-5"),
        ("assess", "ok", "claude-haiku-5-5"),
    ]
    # Cost from the price table: (412 x 0.10 + 96 x 0.50) / 1e6 USD for extraction, at 1 EUR per USD.
    assert float(calls[0][5]) == round((412 * 0.10 + 96 * 0.50) / 1_000_000, 6)
    assert all(c[6] == CORRELATION_ID and c[7] == 1 for c in calls)
    assert states == [
        "submitted", "extracting", "awaiting_confirmation", "searching", "retrieving",
        "assessing", "verifying", "scoring", "done",
    ]  # fmt: skip
