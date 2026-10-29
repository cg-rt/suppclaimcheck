"""PubMed query (P1.08, P2.12): built by code from the confirmed claim, never by the LLM.

The supplement must appear in the title or abstract, along with at least one word of the effect;
if a population is given, at least one of its words too. The gateway adds the open-access filter.
Only letters, digits, spaces and hyphens from the claim reach the query, so user text can't add
PubMed operators or field tags.

Query v1 (D-042) also searches the supplement's other names from the config's synonym list, so
"ashwagandha" finds articles that only say "Withania somnifera". Without a `query` section in the
config the query is v0's.
"""

import re

from scc_core.models import Claim

V0 = "query-v0"
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


def version(cfg: dict | None) -> str:
    return (cfg or {}).get("version", V0)


def keywords(text: str | None) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for word in _clean(text).split():
        if len(word) >= 4 and word not in _STOPWORDS and word not in seen:
            seen.append(word)
    return seen


def names(supplement: str, cfg: dict | None = None) -> list[str]:
    """The supplement as written, then the other names of every synonym group it mentions."""
    written = _clean(supplement)
    found = [written] if written else []
    padded = f" {written} "
    for group in (cfg or {}).get("synonyms", []):
        cleaned = [_clean(n) for n in group]
        if any(n and f" {n} " in padded for n in cleaned):
            found += [n for n in cleaned if n and n not in found]
    return found


def build(claim: Claim, cfg: dict | None = None) -> str:
    supplement = names(claim.supplement, cfg)
    if not supplement:
        raise ValueError("the claim names no supplement")
    terms = [f'"{n}"[tiab]' for n in supplement]
    parts = [terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"]
    for field in (claim.effect, claim.population):
        words = keywords(field)
        if words:
            parts.append("(" + " OR ".join(f"{w}[tiab]" for w in words) + ")")
    return " AND ".join(parts)


def text_query(claim: Claim, cfg: dict | None = None) -> str:
    """The same words as a Postgres websearch query, for ranking passages (OR of all words)."""
    words = (
        [w for n in names(claim.supplement, cfg) for w in n.split()]
        + keywords(claim.effect)
        + keywords(claim.population)
    )
    return " or ".join(dict.fromkeys(words))
