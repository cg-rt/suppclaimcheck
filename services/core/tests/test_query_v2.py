"""Query v2 (D-052): other spellings of effect and population words, and candidates given another way."""

import pytest

from scc_core import config
from scc_core.models import Claim
from scc_core.pipeline import query

V5 = config.load("pipeline-v5")
V6 = config.load("pipeline-v6")
Q = V6["query"]
IRON = Claim(
    supplement="iron", effect="reduces anaemia", population="menstruating women"
)


def test_v6_is_v5_with_query_v2_and_nothing_else():
    assert query.version(Q) == "query-v2"
    assert {k: v for k, v in V6.items() if k not in ("version", "query")} == {
        k: v for k, v in V5.items() if k not in ("version", "query")
    }
    extra = {"version", "spelling_variants", "exclude_title_words"}
    assert {k: v for k, v in Q.items() if k not in extra} == {
        k: v for k, v in V5["query"].items() if k != "version"
    }


def test_a_british_spelling_also_searches_the_american_one():
    assert query.build(IRON, Q) == (
        '"iron"[tiab] AND (anaemia[tiab] OR anemia[tiab])'
        " AND (menstruating[tiab] OR women[tiab])"
    )
    # pipeline-v5 searched the word as written only: the iron regression in docs/EVALS.md.
    assert "anemia" not in query.build(IRON, V5["query"])


def test_and_an_american_spelling_the_british_one():
    claim = Claim(supplement="vitamin A", effect="healthy eyes", population="fetus")
    assert "(fetus[tiab] OR foetus[tiab])" in query.build(claim, Q)


def test_only_whole_words_from_the_table_get_other_spellings():
    claim = Claim(supplement="iron", effect="anaemias and haemoglobinopathy")
    built = query.build(claim, Q)
    assert "(anaemias[tiab] OR haemoglobinopathy[tiab])" in built
    assert "anemia" not in built and "hemoglobin" not in built


def test_a_claim_without_such_words_keeps_the_v1_query():
    claim = Claim(supplement="Ashwagandha", effect="reduces stress and anxiety")
    assert query.build(claim, Q) == query.build(claim, V5["query"])


def test_passage_ranking_words_include_the_other_spellings():
    assert (
        query.text_query(IRON, Q)
        == "iron or anaemia or anemia or menstruating or women"
    )


@pytest.mark.parametrize("entry", ['anemia"[mh] OR (x', "red cells", "Anemia"])
def test_a_spelling_entry_must_be_one_lowercase_word(entry):
    cfg = {**Q, "spelling_variants": [["anaemia", entry]]}
    with pytest.raises(ValueError, match="not one lowercase word"):
        query.build(IRON, cfg)


def candidates():
    return [
        {"pmid": "1", "title": "Oral iron for anaemia in women: a systematic review"},
        {
            "pmid": "2",
            "title": "Intravenous versus oral iron for anaemia among pregnant women",
        },
        {"pmid": "3", "title": "Iron infusions before surgery"},
        {
            "pmid": "4",
            "title": "Iron injection in piglets",
            "skip_reason": "licence_not_allowed",
        },
        {"pmid": "5", "title": "Iron status and intravenously-fed preterm infants"},
        {"pmid": "6", "title": None},
    ]


def test_candidates_given_by_injection_or_infusion_are_marked_not_read():
    articles, marked = query.other_route(candidates(), IRON, Q)
    reasons = {a["pmid"]: a.get("skip_reason") for a in articles}
    assert reasons == {
        "1": None,
        "2": query.NOT_BY_MOUTH,
        "3": query.NOT_BY_MOUTH,  # infusion* matches "infusions"
        "4": "licence_not_allowed",  # an earlier reason stays and isn't counted
        "5": query.NOT_BY_MOUTH,  # "intravenously-fed": a hyphen splits the words
        "6": None,
    }
    assert marked == 3
    assert [a["pmid"] for a in articles] == ["1", "2", "3", "4", "5", "6"]


@pytest.mark.parametrize(
    "field, value",
    [
        ("dose", "monthly injections"),
        ("effect", "raises ferritin faster as an infusion"),
        ("supplement", "intravenous iron"),
    ],
)
def test_a_claim_that_names_the_route_keeps_those_articles(field, value):
    claim = IRON.model_copy(update={field: value})
    articles, marked = query.other_route(candidates(), claim, Q)
    assert marked == 0 and articles == candidates()


def test_without_the_setting_nothing_is_marked():
    articles, marked = query.other_route(candidates(), IRON, V5["query"])
    assert (articles, marked) == (candidates(), 0)


def test_a_title_word_must_be_a_plain_word():
    with pytest.raises(ValueError, match="not a plain word"):
        query.other_route(candidates(), IRON, {"exclude_title_words": ["iv OR x"]})
