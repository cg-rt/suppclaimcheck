"""P1.08 parts that need no database: query v0, JATS passages, rubric v0, strict schemas."""

import json
from pathlib import Path

import pytest

from scc_core import config
from scc_core.models import Assessment, Claim, Extraction
from scc_core.pipeline import jats, query, rubric
from scc_core.pipeline.run import strict_schema

FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "canned_claim"
    / "literature"
)
CFG = config.load(
    "pipeline-v0"
)  # these tests pin v0; rubric v1 has its own (test_rubric_v1.py)


def test_query_is_built_by_code_from_the_claim():
    claim = Claim(
        supplement="Creatine",
        effect="increases muscle strength",
        population="older adults",
    )
    assert (
        query.build(claim)
        == '"creatine"[tiab] AND (muscle[tiab] OR strength[tiab]) AND (older[tiab] OR adults[tiab])'
    )


def test_user_text_cannot_inject_pubmed_syntax():
    claim = Claim(supplement='zinc"[mesh] OR (cancer', effect="helps colds) OR 1[uid]")
    built = query.build(claim)
    assert built.count('"') == 2 and "[mesh]" not in built and "[uid]" not in built
    assert built.startswith('"zinc mesh or cancer"[tiab]')


def test_jats_passages_from_the_recorded_article():
    index = json.loads((FIXTURES / "index.json").read_text("utf-8"))
    xml = (
        FIXTURES / next(v["file"] for k, v in index.items() if "fullTextXML" in k)
    ).read_text("utf-8")
    passages = jats.passages(
        xml, CFG["retrieval"]["passage_max_chars"], CFG["retrieval"]["skip_sections"]
    )
    assert passages[0].section == "Abstract"
    assert all(p.text and "  " not in p.text for p in passages)
    assert not any("Author contributions" in p.section for p in passages)
    assert [p.ordinal for p in passages] == list(range(len(passages)))


R = CFG["rubric"]


@pytest.mark.parametrize(
    ("articles", "supports", "contradicts", "verdict", "score"),
    [
        (0, 0, 0, rubric.NOT_FOUND, 50),
        (1, 0, 0, rubric.INSUFFICIENT, 50),
        (1, 6, 0, rubric.SUPPORTED, 100),
        (1, 3, 0, rubric.SUPPORTED, 75),
        (1, 0, 3, rubric.CONTRADICTED, 25),
        (1, 2, 2, rubric.MIXED, 50),
        (1, 4, 2, rubric.MIXED, 67),
    ],
)
def test_rubric_v0(articles, supports, contradicts, verdict, score):
    result = rubric.score(articles, supports, contradicts, R)
    assert (result.verdict, result.score) == (verdict, score)


@pytest.mark.parametrize("model", [Extraction, Assessment])
def test_llm_schemas_are_strict_and_self_contained(model):
    schema = strict_schema(model)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert schema["additionalProperties"] is False
    item = schema["properties"][next(iter(schema["properties"]))]["items"]
    assert item["additionalProperties"] is False and set(item["required"]) == set(
        item["properties"]
    )
