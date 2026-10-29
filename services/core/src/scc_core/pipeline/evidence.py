"""Evidence tiers and article ranking (P2.05). Code only: no LLM decides what kind of study an article is.

Each article gets one tier from its PubMed publication types and MeSH headings, checked strongest first.
Articles not yet indexed in MEDLINE have no MeSH headings, so a few title words are the last resort.
The tier's weight (versioned config) feeds rubric v1; the ranking picks which N articles are read.
"""

import re

SYSTEMATIC = {
    "meta-analysis",
    "systematic review",
    "systematic-review",
    "network meta-analysis",
}
RCT = {"randomized controlled trial"}
TRIAL_PREFIX = (
    "clinical trial",
    "controlled clinical trial",
    "pragmatic clinical trial",
)
OBSERVATIONAL_TYPES = {"observational study"}
OBSERVATIONAL_MESH = {
    "cohort studies",
    "prospective studies",
    "retrospective studies",
    "cross-sectional studies",
    "case-control studies",
    "longitudinal studies",
}
REVIEW = {
    "review",
    "review-article",
    "scoping review",
    "practice guideline",
    "guideline",
    "consensus statement",
}
# No results of their own (protocols, opinion) or a single patient (case reports).
OTHER = {
    "editorial",
    "letter",
    "comment",
    "case reports",
    "case-report",
    "news",
    "erratum",
    "clinical trial protocol",
}
IN_VITRO_MESH = {
    "in vitro techniques",
    "cells, cultured",
    "cell line",
    "cell line, tumor",
}

_ANIMAL_WORDS = re.compile(
    r"\b(rats?|mice|mouse|murine|rodents?|zebrafish|elegans|drosophila|piglets?|broilers?|"
    r"chickens?|rabbits?|dogs?|canine|bovine|cattle|sheep|catfish)\b",
    re.IGNORECASE,
)
_IN_VITRO_WORDS = re.compile(
    r"\b(in vitro|cell lines?|cultured cells|in silico)\b", re.IGNORECASE
)
_HUMAN_WORDS = re.compile(
    r"\b(patients|participants|volunteers|adults|women|men|children|adolescents|athletes|"
    r"humans|pregnan\w*|elderly|older people)\b",
    re.IGNORECASE,
)

TIERS = (
    "systematic_review",
    "rct",
    "human_trial",
    "observational",
    "human_other",
    "narrative_review",
    "unclassified",
    "animal",
    "in_vitro",
    "other",
)


def tier(article: dict) -> str:
    """The article's evidence tier, from publication types, then MeSH headings, then title words."""
    types = {t.casefold() for t in article.get("pub_types") or []}
    mesh = {m.casefold() for m in article.get("mesh") or []}
    title = article.get("title") or ""
    if types & SYSTEMATIC:
        return "systematic_review"
    if types & RCT:
        return "rct"
    if any(t.startswith(TRIAL_PREFIX) for t in types - {"clinical trial protocol"}):
        return "human_trial"
    if types & OBSERVATIONAL_TYPES or mesh & OBSERVATIONAL_MESH:
        return "observational"
    if types & REVIEW:
        return "narrative_review"
    if types & OTHER:
        return "other"
    if mesh:
        if "humans" in mesh:
            return (
                "in_vitro"
                if mesh & IN_VITRO_MESH and "animals" not in mesh
                else "human_other"
            )
        if "animals" in mesh:
            return "animal"
        if mesh & IN_VITRO_MESH:
            return "in_vitro"
    if _IN_VITRO_WORDS.search(title):
        return "in_vitro"
    if _ANIMAL_WORDS.search(title):
        return "animal"
    if _HUMAN_WORDS.search(title):
        return "human_other"
    return "unclassified"


def rank(
    articles: list[dict], weights: dict[str, float] | None, mode: str
) -> list[dict]:
    """Assessable articles, best first, each with its tier and weight.

    mode "tier": higher tier weight first, PubMed relevance (the given order) breaks ties.
    mode "relevance": PubMed relevance only (pipeline v0).
    Articles the gateway marked with a skip reason (licence, no full text) are left out.
    Without weights (pipeline v0) every tier weighs 1.
    """
    weights = weights or dict.fromkeys(TIERS, 1.0)
    if mode not in {"tier", "relevance"}:
        raise ValueError(f"unknown ranking mode {mode!r}")
    candidates = []
    for position, article in enumerate(articles):
        if article.get("skip_reason"):
            continue
        t = tier(article)
        candidates.append((position, {**article, "tier": t, "weight": weights[t]}))
    if mode == "tier":
        candidates.sort(key=lambda item: (-item[1]["weight"], item[0]))
    return [article for _, article in candidates]
