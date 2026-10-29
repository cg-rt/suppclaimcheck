"""P2.12 acceptance: query v1 (other names from the synonym list) and the spelling suggestion step."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx

from scc_core import config
from scc_core.gateway_client import GatewayError
from scc_core.models import Claim
from scc_core.pipeline import query, run

Q = config.load("pipeline-v2")["query"]


def test_v2_is_active_and_uses_query_v1():
    assert config.ACTIVE == "pipeline-v2"
    assert query.version(Q) == "query-v1"
    assert query.version(config.load("pipeline-v1").get("query")) == "query-v0"


def test_common_name_also_searches_the_latin_name():
    claim = Claim(supplement="Ashwagandha", effect="reduces stress and anxiety")
    assert query.build(claim, Q) == (
        '("ashwagandha"[tiab] OR "withania somnifera"[tiab])'
        " AND (stress[tiab] OR anxiety[tiab])"
    )


def test_a_name_inside_a_longer_supplement_brings_its_group():
    assert query.names("Ashwagandha root extract", Q) == [
        "ashwagandha root extract",
        "ashwagandha",
        "withania somnifera",
    ]


def test_the_latin_name_brings_the_common_one():
    assert query.names("Withania somnifera", Q) == ["withania somnifera", "ashwagandha"]


def test_a_word_inside_another_word_does_not_match():
    # "vitamin c" must not match "vitamin c3" or similar; whole words only.
    assert query.names("vitamin c3", Q) == ["vitamin c3"]


def test_no_synonym_group_leaves_the_v0_query():
    claim = Claim(supplement="Creatine", effect="increases muscle strength")
    assert (
        query.build(claim, Q)
        == query.build(claim)
        == ('"creatine"[tiab] AND (muscle[tiab] OR strength[tiab])')
    )


def test_synonyms_cannot_be_used_to_inject_syntax():
    claim = Claim(supplement='turmeric"[mesh] OR (x', effect="helps joints")
    built = query.build(claim, Q)
    assert "[mesh]" not in built and "(x" not in built
    # Every name is a quoted [tiab] phrase made of cleaned words only.
    assert built.startswith(
        '("turmeric mesh or x"[tiab] OR "turmeric"[tiab] OR "curcumin"[tiab] OR "curcuma longa"[tiab])'
    )


def test_passage_ranking_words_include_the_other_names():
    claim = Claim(supplement="ashwagandha", effect="reduces stress")
    assert (
        query.text_query(claim, Q) == "ashwagandha or withania or somnifera or stress"
    )


class FakeGateway:
    def __init__(self, answers: dict):
        self.answers = answers
        self.asked: list[str] = []

    async def spell(self, term: str) -> dict:
        self.asked.append(term)
        answer = self.answers[term]
        if isinstance(answer, Exception):
            raise answer
        return {"term": term, "suggestion": answer}


def test_spelling_suggestion_is_added_to_each_claim_once_per_name():
    gateway = FakeGateway({"ashwaganda": "ashwagandha", "zinc": None})
    claims = [
        {"supplement": "ashwaganda", "effect": "stress"},
        {"supplement": "ashwaganda", "effect": "sleep"},
        {"supplement": "zinc", "effect": "colds"},
    ]
    asyncio.run(run._spelling(SimpleNamespace(gateway=gateway), uuid4(), claims))
    assert [c["spelling_suggestion"] for c in claims] == [
        "ashwagandha",
        "ashwagandha",
        None,
    ]
    assert gateway.asked == ["ashwaganda", "zinc"]
    # A suggestion is only offered: the supplement stays as the text named it.
    assert claims[0]["supplement"] == "ashwaganda"


def test_a_failed_spelling_check_leaves_no_suggestion():
    gateway = FakeGateway(
        {
            "a": GatewayError(503, "literature_unavailable"),
            "b": httpx.ConnectError("down"),
        }
    )
    claims = [{"supplement": "a", "effect": "x"}, {"supplement": "b", "effect": "y"}]
    asyncio.run(run._spelling(SimpleNamespace(gateway=gateway), uuid4(), claims))
    assert [c["spelling_suggestion"] for c in claims] == [None, None]
