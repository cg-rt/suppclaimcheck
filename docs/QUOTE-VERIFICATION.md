# Quote verification

The product's core promise: a quote is shown only if it appears word for word in the passage it cites (AGENTS.md section 8, P1.09). Code checks every quote the model returns. A quote that fails is never shown; it is dropped and stored as a hallucination event (`core.hallucination_events`), with the passage, the quote, the reason and the config version.

## The check

The quote and the passage go through the same normalisation, then the quote must appear inside the passage. The passage text is exactly the text the model was given.

Normalisation removes differences in how the same characters are encoded or typeset. It never forgives a changed, added or missing word. In order:

1. **Unicode NFKC:** ligatures (the single character "ﬁ" becomes "fi"), full-width and compatibility forms, and non-breaking spaces become their plain forms.
2. **Invisible characters are removed:** soft hyphen, zero-width space and joiners, and the byte-order mark.
3. **Typographic quotes become straight:** ‘ ’ ‚ ‛ ′ become `'`; “ ” „ ‟ ″ become `"`.
4. **Every dash or hyphen variant becomes `-`:** hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar and minus sign.
5. **Whitespace collapses:** runs of spaces, tabs and line breaks become one space, and leading and trailing spaces are dropped.

| Passes | Fails |
|---|---|
| A different dash or quote character | One word changed, added or removed |
| A line break where the article has a space | A number changed (0.35 → 0.53) |
| "fi" written out where the article uses a ligature | Words skipped with "..." or "…" |
| | Different capital letters |
| | Shorter than 20 characters (config `quotes.min_chars`) |
| | Cites a passage the model was not given |

## Where it lives

- Code: `services/core/src/scc_core/pipeline/quotes.py`.
- Tests: `services/core/tests/test_quotes.py`, including near misses. The end-to-end test plants a fabricated quote and checks it is dropped (`tests/integration/test_canned_claim.py`).
- The minimum length is pipeline config (`pipeline-v0.toml`). Changing it needs an eval run from Phase 2 on.
