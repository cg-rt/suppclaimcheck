"""P3.11: the method page's numbers are core's. Web doesn't read core's config, so these tests keep
`scc_web.pages.METHOD` equal to the active pipeline config, and the page's worked example equal to
what core's own rubric gives for the same three articles. A new active pipeline version that changes
any of them fails here until the page says the same."""

from scc_core import config as core_config
from scc_core.pipeline import rubric
from scc_web.pages import METHOD, worked_example


def test_the_method_page_numbers_match_the_active_pipeline_config():
    cfg = core_config.load(core_config.ACTIVE)
    assert METHOD["pipeline"] == core_config.ACTIVE
    assert METHOD["rubric"] == cfg["rubric"]["version"]
    assert METHOD["considered"] == cfg["search"]["pubmed_max_results"]
    assert METHOD["read"] == cfg["search"]["articles_to_assess"]
    assert METHOD["passages"] == cfg["retrieval"]["top_k"]
    assert cfg["retrieval"]["mode"] == "hybrid"
    assert METHOD["keyword_weight"] == cfg["retrieval"]["keyword_weight"]
    assert METHOD["weights"] == cfg["evidence"]["weights"]
    assert set(METHOD["human_tiers"]) == set(cfg["evidence"]["human_tiers"])
    assert METHOD["full_strength"] == cfg["rubric"]["full_strength_weight"]
    assert METHOD["min_strength"] == cfg["rubric"]["min_strength"]
    assert METHOD["mixed_share"] == cfg["rubric"]["mixed_min_share"]
    assert METHOD["reuse_days"] == cfg["cache"]["result_days"]
    assert METHOD["page_chars"] == cfg["inputs"]["url_max_chars"]


def test_the_worked_example_is_what_the_rubric_gives():
    cfg = core_config.load(core_config.ACTIVE)
    w = cfg["evidence"]["weights"]
    articles = [
        rubric.ArticleEvidence(
            "PMC1", "systematic_review", w["systematic_review"], 1, 0
        ),
        rubric.ArticleEvidence("PMC2", "rct", w["rct"], 1, 0),
        rubric.ArticleEvidence("PMC3", "narrative_review", w["narrative_review"], 0, 1),
    ]
    scored = rubric.score_v1(
        articles, {**cfg["rubric"], "human_tiers": cfg["evidence"]["human_tiers"]}
    )
    example = worked_example(METHOD)
    assert example["score"] == scored.score
    assert round(example["direction"], 3) == scored.direction
    assert scored.verdict == ("Mixed" if example["mixed"] else "Supported")
