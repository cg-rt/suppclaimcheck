"""P2.06 acceptance: hybrid passage ranking against a real database, and the embedding cache.

A fake embedder stands in for the model so the test can say exactly which passage is "close in
meaning" to the claim; the real model has its own test (services/core/tests/test_embeddings.py).
"""

import httpx
from psycopg_pool import AsyncConnectionPool

from scc_core import config as core_config
from scc_core import jobs
from scc_core.gateway_client import GatewayClient
from scc_core.models import Claim, JobCreate
from scc_core.pipeline import run
from tests.helpers import run_async

# Passage 0 shares the claim's words but says nothing about the effect; passage 2 reports the effect
# in other words. Keyword ranking prefers 0, the fake embedder prefers 2.
PARAGRAPHS = [
    "Fish oil capsules and triglycerides were listed among the study materials and labels.",
    "Participants were recruited from two outpatient clinics between 2015 and 2017.",
    "Omega-3 supplementation reduced serum triacylglycerol concentrations versus placebo.",
]
XML = (
    "<article><body>"
    + "".join(
        f"<sec><title>Section {i}</title><p>{p}</p></sec>"
        for i, p in enumerate(PARAGRAPHS)
    )
    + "</body></article>"
)


class FakeEmbedder:
    """384-dimension vectors: the claim and passages mentioning "triacylglycerol" point the same way."""

    name = "fake-embedder"

    def __init__(self):
        self.embedded: list[str] = []

    @staticmethod
    def _vector(close: bool) -> list[float]:
        v = [0.0] * 384
        v[0 if close else 1] = 1.0
        return v

    def passages(self, texts: list[str]) -> list[list[float]]:
        self.embedded += texts
        return [self._vector("triacylglycerol" in t) for t in texts]

    def query(self, text: str) -> list[float]:
        return self._vector(True)


def test_hybrid_ranking_and_embedding_cache(migrated_db):
    cfg = core_config.load("pipeline-v3")
    cfg = {**cfg, "retrieval": {**cfg["retrieval"], "top_k": 2, "keyword_weight": 0.3}}
    claim = Claim(supplement="fish oil", effect="lowers triglycerides")
    embedder = FakeEmbedder()

    async def scenario():
        pool = AsyncConnectionPool(migrated_db.conninfo("core"), open=False)
        await pool.open()
        try:
            rt = run.Runtime(
                pool,
                GatewayClient(
                    "http://gateway",
                    transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                ),
                cfg,
                embedder,
            )
            ranked = []
            for key in ("first-check", "second-check"):
                async with pool.connection() as conn:
                    job, _ = await jobs.create(
                        conn,
                        JobCreate(
                            user_id=1, input_text="Fish oil lowers triglycerides."
                        ),
                        key,
                        cfg["version"],
                        None,
                    )
                article = {"pmcid": "PMC1", "pmid": "1"}
                ranked.append(
                    await run._rank_passages(
                        rt, job.id, claim, article, XML, embedder.query("")
                    )
                )
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT count(*) FROM core.embeddings WHERE model = 'fake-embedder'"
                )
                stored = (await cur.fetchone())[0]
            return ranked, stored
        finally:
            await pool.close()

    (first, second), stored = run_async(scenario())
    # Keyword rank alone would put the label-list paragraph first; fusion lifts the finding to the top.
    assert first[0]["text"] == PARAGRAPHS[2]
    assert len(first) == 2
    # The same article in a second check: same ranking, and no passage embedded again (cache hit).
    assert [p["text"] for p in second] == [p["text"] for p in first]
    assert len(embedder.embedded) == len(PARAGRAPHS) == stored
