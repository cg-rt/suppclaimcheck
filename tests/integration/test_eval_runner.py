"""P2.02/P2.03 acceptance: the eval runner records a live run, replays it for free, and notices staleness.

The "live" gateway here is the real gateway app in-process, answering from the canned-claim fixtures
(test_canned_claim.py), so the test needs no network and no key. It walks the life of a recording:
1. a live run records every core-to-gateway reply;
2. a replay gives the same verdict and score without calling the gateway;
3. a rubric change (score weights) replays fine and shows the new score;
4. a prompt change makes the recorded replies stale, so only a new live run can refresh them.
"""

import copy

import httpx
import psycopg
from psycopg_pool import AsyncConnectionPool

from scc_common.recorded import ReplayTransport
from scc_core import config as core_config
from scc_core.eval.cases import EvalCase
from scc_core.eval.replay import EvalTransport
from scc_core.eval.runner import run_cases, scratch_database
from scc_gateway import config as gateway_config
from scc_gateway import main as gateway_main
from scc_gateway.literature import Literature
from scc_gateway.llm import ClaudeProvider
from tests.conftest import ADMIN_DSN
from tests.helpers import run_async
from tests.integration.test_canned_claim import (
    FIXTURES,
    SUBMITTED,
    anthropic_fixture_client,
)

CASE = EvalCase.model_validate(
    {
        "id": "canned-01",
        "category": "well_supported",
        "input": SUBMITTED,
        "expected": {
            "behaviour": "answer",
            "verdicts": ["Supported"],
            "score_range": [60, 100],
            "supplement": ["creatine"],
        },
        "label": {
            "rationale": "The canned claim; its fixtures hold three supporting quotes.",
            "pmids": [],
            "status": "draft",
            "drafted_on": "2026-10-28",
        },
    }
)


def test_record_replay_and_staleness(migrated_db, tmp_path):
    store = tmp_path / "recording"

    async def run(transport: EvalTransport, config: dict, run_id: str):
        results = await run_cases(
            [CASE],
            config,
            transport,
            migrated_db.conninfo("core"),
            run_id,
            progress=lambda _: None,
        )
        return results[0]

    async def live_run():
        pool = AsyncConnectionPool(migrated_db.conninfo("gateway"), open=False)
        await pool.open()
        try:
            gcfg = gateway_config.load()
            gateway_app = gateway_main.build_app(
                checks={},
                deps=gateway_main.Deps(
                    pool,
                    ClaudeProvider(
                        "test-key", 30, http_client=anthropic_fixture_client([])
                    ),
                    Literature(
                        gcfg, None, pool, ReplayTransport(FIXTURES / "literature")
                    ),
                    gcfg,
                ),
            )
            transport = EvalTransport(
                "live", store, inner=httpx.ASGITransport(app=gateway_app)
            )
            result = await run(transport, core_config.load(), "live-1")
            transport.save()
            return result
        finally:
            await pool.close()

    live = run_async(live_run())
    assert live.passed, live.reasons
    assert (live.observed["verdict"], live.observed["score"]) == ("Supported", 75)
    assert live.observed["llm_calls"] == 2 and live.observed["cost_eur"] > 0
    assert live.observed["latency_s"] is not None
    assert (
        live.observed["quotes_dropped"] == 1
    )  # the fixture's planted fabricated quote
    # The calls were charged to the eval lane, never to live users' budget.
    with psycopg.connect(migrated_db.admin_conninfo) as conn:
        lanes = conn.execute("SELECT DISTINCT lane FROM gateway.llm_calls").fetchall()
    assert lanes == [("eval",)]

    replayed = run_async(
        run(EvalTransport("replay", store), core_config.load(), "replay-1")
    )
    assert (replayed.observed["verdict"], replayed.observed["score"]) == (
        "Supported",
        75,
    )
    assert (
        replayed.observed["cost_eur"] == live.observed["cost_eur"]
    )  # the recorded cost
    assert replayed.observed["latency_s"] is None  # a replay can't measure latency

    # A rubric change sends the same requests: free to re-check. Full strength at 3 passages: 50 + 50 x 1 x 1.
    rubric_changed = copy.deepcopy(core_config.load())
    rubric_changed["rubric"]["full_strength_passages"] = 3
    rescored = run_async(
        run(EvalTransport("replay", store), rubric_changed, "replay-2")
    )
    assert not rescored.stale and rescored.observed["score"] == 100

    # A prompt change sends a different request: the recording is stale.
    prompt_changed = copy.deepcopy(core_config.load())
    prompt_changed["prompts"]["assess"] += "\nBe brief."
    stale = run_async(run(EvalTransport("replay", store), prompt_changed, "replay-3"))
    assert stale.stale and not stale.passed
    assert "run a live eval" in stale.reasons[0]


def test_scratch_database_is_created_and_dropped():
    from tests.conftest import require_db

    require_db()
    with scratch_database(ADMIN_DSN) as conninfo:
        dbname = psycopg.conninfo.conninfo_to_dict(conninfo)["dbname"]
        with psycopg.connect(conninfo) as conn:
            assert conn.execute("SELECT count(*) FROM core.jobs").fetchone() == (0,)
    with psycopg.connect(ADMIN_DSN) as admin:
        assert (
            admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
            ).fetchone()
            is None
        )
