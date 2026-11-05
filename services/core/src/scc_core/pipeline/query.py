"""PubMed query (P1.08, P2.12): built by code from the confirmed claim, never by the LLM.

The supplement must appear in the title or abstract, along with at least one word of the effect;
if a population is given, at least one of its words too. The gateway adds the open-access filter.
Only letters, digits, spaces and hyphens from the claim reach the query, so user text can't add
PubMed operators or field tags.

Query v1 (D-042) also searches the supplement's other names from the config's synonym list, so
"ashwagandha" finds articles that only say "Withania somnifera". Without a `query` section in the
config the query is v0's.

Query v2 (D-052) adds two things, both from the config:
- each effect and population word is also searched in its other spellings ("anaemia" also finds
  "anemia"): a [tiab] term matches only the word as written, and PubMed doesn't map spellings there;
- candidates whose title says the substance was given another way (intravenous, infusion,
  injection) are not read, since supplements are taken by mouth (`other_route`, applied by the
  pipeline after the search). A claim that itself names one of those ways keeps them.
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
# A title word to exclude: letters and digits, optionally ending in PubMed-style truncation *.
_TITLE_WORD = re.compile(r"[a-z0-9]+\*?")
_TOKEN = re.compile(r"[a-z0-9]+")
# One entry of the spelling table: what keywords() can produce.
_WORD = re.compile(r"[a-z0-9\-]+")
# The skip reason for an article whose title says the substance was injected or infused.
NOT_BY_MOUTH = "not_by_mouth"


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


def spellings(words: list[str], cfg: dict | None = None) -> list[str]:
    """The words, each followed by its other spellings from the config's table (whole words only)."""
    groups = (cfg or {}).get("spelling_variants", [])
    for word in (w for g in groups for w in g):
        if not _WORD.fullmatch(word):
            raise ValueError(f"spelling_variants: {word!r} is not one lowercase word")
    found: list[str] = []
    for word in words:
        found.append(word)
        for group in groups:
            if word in group:
                found += [w for w in group if w and w not in found]
    return list(dict.fromkeys(found))


def _has(word: str, tokens: list[str]) -> bool:
    """Whether a title word from the config (PubMed style, * truncates) is among the tokens."""
    if word.endswith("*"):
        return any(t.startswith(word[:-1]) for t in tokens)
    return word in tokens


def other_route(
    articles: list[dict], claim: Claim, cfg: dict | None = None
) -> tuple[list[dict], int]:
    """The candidates, with those whose title says the substance was given another way (the config's
    `exclude_title_words`) marked `skip_reason = "not_by_mouth"`, and how many were marked.

    A filter on the candidates, not a NOT in the PubMed query, so PubMed's ranking of everything else
    stays as it was. A claim that itself uses one of the words (a claim about injections) marks none.
    """
    words = (cfg or {}).get("exclude_title_words", [])
    for word in words:
        if not _TITLE_WORD.fullmatch(word):
            raise ValueError(f"exclude_title_words: {word!r} is not a plain word")
    fields = (
        claim.supplement,
        claim.effect,
        claim.population,
        claim.dose,
        claim.duration,
    )
    claim_tokens = _TOKEN.findall(" ".join(f for f in fields if f).lower())
    if not words or any(_has(w, claim_tokens) for w in words):
        return articles, 0
    marked = 0
    out = []
    for article in articles:
        title = _TOKEN.findall((article.get("title") or "").lower())
        if not article.get("skip_reason") and any(_has(w, title) for w in words):
            article = {**article, "skip_reason": NOT_BY_MOUTH}
            marked += 1
        out.append(article)
    return out, marked


def build(claim: Claim, cfg: dict | None = None) -> str:
    supplement = names(claim.supplement, cfg)
    if not supplement:
        raise ValueError("the claim names no supplement")
    terms = [f'"{n}"[tiab]' for n in supplement]
    parts = [terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"]
    for field in (claim.effect, claim.population):
        words = spellings(keywords(field), cfg)
        if words:
            parts.append("(" + " OR ".join(f"{w}[tiab]" for w in words) + ")")
    return " AND ".join(parts)


def text_query(claim: Claim, cfg: dict | None = None) -> str:
    """The same words as a Postgres websearch query, for ranking passages (OR of all words)."""
    words = (
        [w for n in names(claim.supplement, cfg) for w in n.split()]
        + spellings(keywords(claim.effect), cfg)
        + spellings(keywords(claim.population), cfg)
    )
    return " or ".join(dict.fromkeys(words))
