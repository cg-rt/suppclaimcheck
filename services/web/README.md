# web

Public site, admin and login (D-011). Owns the `auth` schema; runs as database role `scc_web_app`. Server-rendered with Jinja2; htmx and its SSE extension (vendored in `static/js`) only for live progress.

- **Run:** `python -m scc_web` (port 8000). Migrate step: `python -m scc_web.migrate`.
- **Pages:** `/sign-in`, `/checks/new`, `/checks/{id}` (confirm, progress, result or stopped, by the job's state), `/checks/{id}/events` (server-sent events), `/history`, `/account`. Ops: `/health`, `/health/deps`, `/version`.
- **Accounts:** invite-only, created with the admin CLI inside the container: `python -m scc_web.admin create-user someone@example.com [--admin]`, `set-password`, `disable`. The password is asked for at a hidden prompt, or read from stdin. In dev, `uv run python tools/dev_user.py` creates `tester@example.com`.
- **Config:** `SCC_DB_*` (see `scc_common.db`), `SCC_CORE_URL`, `SCC_TRUST_CLOUDFLARE=1` behind the tunnel (the sign-in rate limit then uses `CF-Connecting-IP`). Session, lockout, password and progress settings are versioned in `src/scc_web/config/web-v0.toml`.
- **Design:** tokens and rules in [`docs/DESIGN.md`](../../docs/DESIGN.md); stylesheet `static/css/app.css`; fonts self-hosted under the SIL Open Font License.
- **Tests:** `uv run pytest services/web` (no database), `tests/integration/test_web_auth.py` (sign-in, lockout, sessions, CSRF, ownership; needs the test database), `tests/e2e` (browser and axe against a running stack; see its docstring).
