# gateway

The only module that calls outside services: the LLM, literature sources and alerts (D-011, D-033). Owns the `gateway` schema; runs as database role `scc_gateway_app`. On the `egress` network.

- **Run:** `python -m scc_gateway` (port 8000). Migrate step: `python -m scc_gateway.migrate`.
- **Endpoints so far:** `/health`, `/health/deps` (database login), `/version`. LLM and literature calls arrive with P1.06 and P1.07.
- **Config:** `SCC_DB_*` (see `scc_common.db`).
- **Tests:** `uv run pytest services/gateway`.
