# core

Jobs, the pipeline, retrieval, quote verification and scoring. One image, two containers (D-011). Owns the `core` schema (jobs, passages, hallucination events and the Procrastinate queue), with pgvector; runs as database role `scc_core_app`.

- **core-api:** `python -m scc_core` (port 8000). `POST /jobs` (needs an `Idempotency-Key` header), `GET /jobs/{id}`, `POST /jobs/{id}/confirm`, plus `/health`, `/health/deps` and `/version`. Spec: `openapi.json`.
- **core-worker:** `python -m scc_core.worker`. Runs the `core:extract_claim` and `core:run_pipeline` tasks from the `pipeline` queue, each under its job's correlation ID. No internet: every outside call goes through the gateway.
- **Pipeline v1** (active; D-044): extract claims (LLM), wait for the user to confirm, build the PubMed query (code), rank the candidates by evidence tier from their metadata (code), fetch the best N = 5 open-access articles, split each into passages, rank them with Postgres full-text search, assess the top K per article (LLM, one call each), check every quote ([docs/QUOTE-VERIFICATION.md](../../docs/QUOTE-VERIFICATION.md)), and score with rubric v1 (code; [docs/METHOD.md](../../docs/METHOD.md)). Pipeline v0 (one article, rubric v0) still runs from its config.
- **Config:** prompts, K, N, evidence weights and rubric numbers in `src/scc_core/config/pipeline-v2.toml`, including the query v1 synonym list (v0 and v1 beside it); `SCC_DB_*` (see `scc_common.db`), `SCC_GATEWAY_URL`.
- **Migrate step:** `python -m scc_core.migrate`.
- **Eval runner:** `uv run python -m scc_core.eval run --mode replay|live`, from the repo root, not in the image's start commands. Runs the cases in `evals/` through the pipeline functions in a throwaway database ([docs/EVALS.md](../../docs/EVALS.md), D-043).
- **Tests:** `uv run pytest services/core`; the end-to-end canned claim is in `tests/integration/test_canned_claim.py`.
