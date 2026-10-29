"""P2.04 rubric v1 and P2.05 evidence tiers and ranking: code decides, so every rule has a test."""

import pytest

from scc_core import config
from scc_core.models import Assessment
from scc_core.pipeline import evidence
from scc_core.pipeline.rubric import ArticleEvidence, score_v1
from scc_core.pipeline.run import ordered, strict_schema

CFG = config.load("pipeline-v1")
RUBRIC = {**CFG["rubric"], "human_tiers": CFG["evidence"]["human_tiers"]}
W = CFG["evidence"]["weights"]


def art(
    tier: str, supporting: int = 0, contradicting: int = 0, pmcid: str = "PMC1"
) -> ArticleEvidence:
    return ArticleEvidence(pmcid, tier, W[tier], supporting, contradicting)


def test_no_articles_is_not_found():
    s = score_v1([], RUBRIC)
    assert (s.verdict, s.score) == ("Not found", 50)


def test_articles_without_verified_findings_are_insufficient():
    s = score_v1([art("rct"), art("systematic_review", pmcid="PMC2")], RUBRIC)
    assert (s.verdict, s.score, s.articles_assessed) == ("Insufficient evidence", 50, 2)


@pytest.mark.parametrize("tier", ["in_vitro", "animal", "unclassified"])
def test_only_lab_animal_or_unclassified_findings_are_insufficient(tier):
    s = score_v1(
        [art(tier, supporting=4), art(tier, supporting=3, pmcid="PMC2")], RUBRIC
    )
    assert s.verdict == "Insufficient evidence"
    assert s.rule.startswith("The only findings come from animal")
    assert 50 < s.score <= 60  # leans the right way, but weakly (2 x 0.2 weight: 60)


def test_one_weak_human_study_is_too_weak_to_call():
    s = score_v1(
        [art("narrative_review", supporting=2)], RUBRIC
    )  # strength 0.3 / 2 = 0.15
    assert (s.verdict, s.strength) == ("Insufficient evidence", 0.15)


def test_one_systematic_review_for_is_supported():
    s = score_v1([art("systematic_review", supporting=3)], RUBRIC)
    assert (s.verdict, s.direction, s.strength, s.score) == ("Supported", 1.0, 0.5, 75)


def test_one_rct_against_is_contradicted():
    s = score_v1([art("rct", contradicting=2)], RUBRIC)
    assert (s.verdict, s.score) == ("Contradicted", 30)  # 50 + 50 x -1 x 0.4


def test_strong_evidence_on_both_sides_is_mixed():
    s = score_v1(
        [
            art("systematic_review", supporting=3),
            art("rct", contradicting=2, pmcid="PMC2"),
        ],
        RUBRIC,
    )
    assert s.verdict == "Mixed"
    assert (s.weight_for, s.weight_against) == (1.0, 0.8)
    assert s.score == 55  # 50 + 50 x (0.2 / 1.8) x 0.9


def test_a_weak_dissent_does_not_make_it_mixed():
    s = score_v1(
        [
            art("systematic_review", supporting=3),
            art("narrative_review", contradicting=1, pmcid="PMC2"),
        ],
        RUBRIC,
    )
    assert s.verdict == "Supported"  # 0.3 of 1.3 against, under the 25% share


def test_quote_count_no_longer_adds_strength():
    # v0's flaw: one article with many quotes looked strong. In v1 one article weighs its tier, once.
    many = score_v1([art("rct", supporting=6)], RUBRIC)
    one = score_v1([art("rct", supporting=1)], RUBRIC)
    assert many.score == one.score == 70


def test_an_article_split_inside_counts_by_its_stance():
    s = score_v1([art("systematic_review", supporting=3, contradicting=1)], RUBRIC)
    assert s.breakdown[0]["stance"] == 0.5
    assert (s.verdict, s.score) == (
        "Supported",
        62,
    )  # weight for 0.5, against 0; 50 + 50 x 0.5 x 0.5


def test_exactly_balanced_findings_are_mixed_not_a_crash():
    s = score_v1([art("rct", supporting=1, contradicting=1)], RUBRIC)
    assert (s.verdict, s.score) == ("Mixed", 50)


def test_strength_is_capped_at_full():
    s = score_v1(
        [art("systematic_review", supporting=1, pmcid=f"PMC{i}") for i in range(4)],
        RUBRIC,
    )
    assert (s.strength, s.score) == (1.0, 100)


# Evidence tiers, from metadata shaped like Europe PMC's.
@pytest.mark.parametrize(
    ("pub_types", "mesh", "title", "expected"),
    [
        (["Meta-Analysis", "Review", "Journal Article"], [], "", "systematic_review"),
        (["review-article", "systematic-review"], [], "", "systematic_review"),
        (
            ["research-article", "Randomized Controlled Trial", "Journal Article"],
            ["Humans"],
            "",
            "rct",
        ),
        (["Clinical Trial, Phase IV", "Journal Article"], [], "", "human_trial"),
        (["Clinical Trial Protocol", "Journal Article"], [], "", "other"),
        (["Journal Article"], ["Humans", "Cohort Studies"], "", "observational"),
        (["Review", "Journal Article"], ["Humans"], "", "narrative_review"),
        (["Journal Article"], ["Animals", "Caenorhabditis elegans"], "", "animal"),
        (["Journal Article"], ["Humans", "Cells, Cultured"], "", "in_vitro"),
        (["Journal Article"], ["Humans", "Adult"], "", "human_other"),
        (
            ["research-article"],
            [],
            "Effects of bee pollen on growth in African catfish",
            "animal",
        ),
        (["research-article"], [], "An in vitro study of curcumin", "in_vitro"),
        (["research-article"], [], "Creatine in older adults: a pilot", "human_other"),
        (["research-article"], [], "A new look at magnesium", "unclassified"),
        (["Case Reports", "Journal Article"], ["Humans"], "", "other"),
    ],
)
def test_evidence_tier(pub_types, mesh, title, expected):
    assert (
        evidence.tier({"pub_types": pub_types, "mesh": mesh, "title": title})
        == expected
    )


def test_ranking_puts_stronger_tiers_first_and_relevance_breaks_ties():
    articles = [
        {"pmid": "1", "pub_types": ["Review"]},
        {"pmid": "2", "pub_types": ["Randomized Controlled Trial"]},
        {
            "pmid": "3",
            "pub_types": ["Meta-Analysis"],
            "skip_reason": "licence_not_allowed",
        },
        {"pmid": "4", "pub_types": ["Randomized Controlled Trial"]},
        {"pmid": "5", "pub_types": ["Systematic Review"]},
    ]
    assert [a["pmid"] for a in evidence.rank(articles, W, "tier")] == [
        "5",
        "2",
        "4",
        "1",
    ]
    assert [a["pmid"] for a in evidence.rank(articles, W, "relevance")] == [
        "1",
        "2",
        "4",
        "5",
    ]
    assert evidence.rank(articles, W, "tier")[0]["weight"] == 1.0
    with pytest.raises(ValueError):
        evidence.rank(articles, W, "newest")


def test_field_order_puts_reason_before_label():
    schema = ordered(
        strict_schema(Assessment), ["passage_id", "reason", "label", "quote"]
    )
    item = schema["properties"]["assessments"]["items"]
    assert list(item["properties"]) == ["passage_id", "reason", "label", "quote"]
    assert item["required"] == ["passage_id", "reason", "label", "quote"]
    # The unchanged default, for comparison: the label comes before the reason.
    assert list(
        strict_schema(Assessment)["properties"]["assessments"]["items"]["properties"]
    ) == [
        "passage_id",
        "label",
        "quote",
        "reason",
    ]
