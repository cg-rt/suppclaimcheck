# core

Jobs, the pipeline, retrieval, quote verification and scoring. One image, two containers (D-011). Owns the `core` schema, with pgvector; runs as database role `scc_core_app`.

- **core-api:** `python -m scc_core` (port 8000). Endpoints so far: `/health`, `/health/deps` (database login, then gateway), `/version`. Jobs arrive with P1.05.
- **core-worker:** `python -m scc_core.worker`. A placeholder that keeps a heartbeat file fresh for its health check, until P1.05 brings the Procrastinate worker. No internet access.
- **Migrate step:** `python -m scc_core.migrate`.
- **Config:** `SCC_DB_*` (see `scc_common.db`), `SCC_GATEWAY_URL`.
- **Tests:** `uv run pytest services/core`.
