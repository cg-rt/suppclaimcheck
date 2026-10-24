# Canned claim fixtures

The end-to-end test (`tests/integration/test_canned_claim.py`) runs one claim through the whole pipeline with no live calls (AGENTS.md section 7).

**Submitted text:** "Taking creatine while doing resistance training makes older adults stronger. Ask your coach about it!"

## `literature/`: recorded, real responses

Recorded on 2026-10-24 by `uv run python tools/record_fixtures.py`, which runs the gateway's own literature code through a recording transport. `index.json` maps each request (method and URL, never an API key) to its file.

- PubMed E-utilities `esearch` for the claim's query v0, with the open-access filter.
- Europe PMC metadata for the 20 PMIDs it returned. Fields the gateway never reads (abstracts, keywords, MeSH terms, affiliations) are removed.
- Europe PMC JATS full text of **PMC7871530**: Antonio J, Candow DG, Forbes SC, et al. *Common questions and misconceptions about creatine supplementation: what does the scientific evidence really show?* Journal of the International Society of Sports Nutrition, 2021; 18:13. doi:10.1186/s12970-021-00412-w. Licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); stored unchanged.

To refresh them, delete `literature/` and run the recorder again. It makes live calls, so it's never run in CI.

## `llm/`: hand-written responses in the API's format

`extract.json` and `assess.json` are hand-written in the shape of Anthropic Messages API replies, because no API key was available in development when they were made. The quotes in `assess.json` were cut programmatically from the real passages, so they are verbatim. One quote, for passage `PMC7871530-31`, is fabricated on purpose: the test checks that verification drops it and records a hallucination event.

Replace these with recorded replies once a dev API key is in place (C1.1). The test reads whatever these files contain, so only the expected numbers in the test need updating.
