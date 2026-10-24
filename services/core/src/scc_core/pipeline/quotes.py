"""Quote verification (P1.09): a quote is shown only if it appears word for word in the passage it cites.

Both the quote and the passage go through the same normalisation, then the quote must be a substring
of the passage. Normalisation only removes differences in how the same characters are encoded or
typeset; it never forgives a changed, added or missing word. The rules, in order:

1. Unicode NFKC: ligatures (fi -> fi), full-width and compatibility forms, non-breaking spaces.
2. Invisible characters are removed: soft hyphen, zero-width space and joiners, byte-order mark.
3. Curly and typographic quotes become straight quotes (' and ").
4. Every dash or hyphen variant (hyphen, non-breaking hyphen, figure, en and em dash, minus) becomes "-".
5. Runs of whitespace, line breaks included, become one space; leading and trailing spaces are dropped.

Case is kept: a quote with different capitals is not verbatim. An ellipsis ("..." or "…") is not
expanded, so a quote that skips words fails. Quotes shorter than the configured minimum fail, so a
two-word "quote" can't pass as evidence. Documented in docs/QUOTE-VERIFICATION.md.
"""

import re
import unicodedata
from dataclasses import dataclass

_INVISIBLE = dict.fromkeys(map(ord, "­\u200b‌‍⁠﻿"), None)
_QUOTES = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "″": '"',
    }
)
_DASHES = re.compile("[‐‑‒–—―−﹘﹣－]")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_INVISIBLE).translate(_QUOTES)
    text = _DASHES.sub("-", text)
    return _SPACE.sub(" ", text).strip()


@dataclass(frozen=True)
class QuoteCheck:
    verified: bool
    reason: str  # "ok", "too_short", "not_in_passage"


def verify_quote(quote: str, passage: str, min_chars: int) -> QuoteCheck:
    wanted = normalise(quote)
    if len(wanted) < min_chars:
        return QuoteCheck(False, "too_short")
    if wanted in normalise(passage):
        return QuoteCheck(True, "ok")
    return QuoteCheck(False, "not_in_passage")
