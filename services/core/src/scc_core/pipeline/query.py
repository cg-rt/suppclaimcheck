"""PubMed query v0 (P1.08): built by code from the confirmed claim, never by the LLM.

The supplement must appear in the title or abstract, along with at least one word of the effect;
if a population is given, at least one of its words too. The gateway adds the open-access filter.
Only letters, digits, spaces and hyphens from the claim reach the query, so user text can't add
PubMed operators or field tags.
"""

import re

from scc_core.models import Claim

VERSION = "query-v0"
# Words that say how an effect is claimed, not what the effect is.
_STOPWORDS = {
    "about", "after", "also", "among", "and", "been", "being", "boost", "boosts", "can", "does",
    "combined", "combination", "during", "effect", "effects", "from", "have", "help", "helps", "improve", "improved", "improves",
    "increase", "increased", "increases", "into", "lower", "lowers", "make", "makes", "more", "people",
    "reduce", "reduced", "reduces", "support", "supports", "taking", "than", "that", "their", "them",
    "they", "this", "when", "with", "while", "your",
}  # fmt: skip
_UNSAFE = re.compile(r"[^a-z0-9\- ]+")


def _clean(text: str) -> str:
    return " ".join(_UNSAFE.sub(" ", text.lower()).split())


def keywords(text: str | None) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for word in _clean(text).split():
        if len(word) >= 4 and word not in _STOPWORDS and word not in seen:
            seen.append(word)
    return seen


def build(claim: Claim) -> str:
    supplement = _clean(claim.supplement)
    if not supplement:
        raise ValueError("the claim names no supplement")
    parts = [f'"{supplement}"[tiab]']
    for field in (claim.effect, claim.population):
        words = keywords(field)
        if words:
            parts.append("(" + " OR ".join(f"{w}[tiab]" for w in words) + ")")
    return " AND ".join(parts)


def text_query(claim: Claim) -> str:
    """The same words as a Postgres websearch query, for ranking passages (OR of all words)."""
    words = (
        _clean(claim.supplement).split()
        + keywords(claim.effect)
        + keywords(claim.population)
    )
    return " or ".join(dict.fromkeys(words))
