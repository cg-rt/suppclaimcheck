# web

Public site, admin and login (D-011). Owns the `auth` schema; runs as database role `scc_web_app`.

- **Run:** `python -m scc_web` (port 8000). Migrate step: `python -m scc_web.migrate`.
- **Endpoints so far:** `/health`, `/health/deps` (database login, then core-api), `/version`. Pages arrive with P1.04 and P1.10.
- **Config:** `SCC_DB_*` (see `scc_common.db`), `SCC_CORE_URL`.
- **Tests:** `uv run pytest services/web`.
