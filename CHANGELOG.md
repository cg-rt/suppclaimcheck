# Changelog

One version for the whole app (D-034). Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- `backup.sh` waits up to 10 minutes for the backup storage host to resolve before starting. A nightly backup missed while the host PC was off runs right at boot, and on 2026-10-26 it failed because DNS through the Hyper-V Default Switch was not answering yet. Host script only: copy it to the VM; no new images.
- `restore.sh` prints the scratch-database drop command with `sudo`, which the VM needs.

## [0.1.2] - 2026-10-25

The first upgrade of the live site, with fixes found during the first production deploy.

### Security
- certifi 2026.10.19 (CA bundle, GHSA-6q48-6c7w-9gw2) in the web, core and gateway images, taken before it is two weeks old (D-041); its scan exception is removed.

### Fixed
- `backup.sh` alerts (Telegram and healthchecks.io) when `secrets/restic.env` is missing or not valid shell. Before, bash stopped on the syntax error before the alert was set up, so the backup failed silently. The alert text is fixed, because bash's own message quotes the bad line, which may hold a secret.
- `/health` answers `HEAD` with 200 instead of 405, for uptime monitors that probe with HEAD.
- core-worker's job start and end log lines carry the job's correlation ID in the `correlation_id` field, not only inside the message.

### Changed
- `deploy/README.md`: the `restic.env` example has no bracket placeholders to paste by mistake, and notes the EU endpoint for EU-jurisdiction buckets; the Telegram row says to press Start in the bot's chat first.

## [0.1.1] - 2026-10-25

The first release. `v0.1.0` was tagged but never released: its workflow logged out of GHCR before the attestation step, so only an unsigned edge image was pushed (`suppclaimcheck-edge:0.1.0`); do not deploy it.

### Added
- Walking-skeleton modules: `edge` (Cloudflare Tunnel), `web`, `core` (`core-api` and a placeholder `core-worker`), `gateway`, `postgres` (PostgreSQL 18 + pgvector). Each has a Dockerfile with a base image pinned by digest and a non-root user.
- `/health`, `/health/deps` and `/version` on every HTTP module; JSON logs with a correlation ID passed between modules (`X-Correlation-ID`).
- Development and production compose files: an internal network with no internet, egress only for `edge` and `gateway`, Docker secrets, no host ports in production.
- One database schema and least-privilege role per module, with per-module Alembic migrations run as a one-off step; `auth.users` table.
- Jobs API in core-api (`POST /jobs` with an `Idempotency-Key`, `GET /jobs/{id}`, `POST /jobs/{id}/confirm`) and a Procrastinate worker in core-worker; jobs carry their correlation ID.
- Pipeline v0: claim extraction, confirmation, PubMed query by code, one open-access article from Europe PMC, JATS passages ranked by Postgres full-text search, passage assessment, verbatim quote verification with hallucination events, and rubric v0 for the verdict and score.
- Gateway: schema-constrained Claude calls with a monthly cap per lane that fails closed, cost from a versioned price table, retries on transient errors only, and a record of every call; PubMed and Europe PMC with the licence allowlist, a rate limiter and an article cache.
- Committed OpenAPI specs for web, core and gateway, checked against the code in CI.
- Login in web: invite-only accounts from an admin CLI (`python -m scc_web.admin`), Argon2id hashes, server-side sessions in a `__Host-` cookie with idle and absolute timeouts, CSRF tokens on every form, sign-in lockout and rate limit, password change (D-039).
- Web pages: sign in, new check, confirm the claim, live progress over server-sent events, result (verdict, score with its arithmetic, verified quotes with citation and licence, what was searched, what could not be checked, not-medical-advice notice), history and account. Design tokens in `docs/DESIGN.md`.
- core-api: `GET /jobs?user_id=` (history) and `GET /jobs/{id}/events` (progress).
- Browser smoke test with axe accessibility checks (`tests/e2e`), run against a running stack.
- Release workflow: a `vX.Y.Z` tag builds all five images, scans them with Trivy (fixable critical or high findings fail it, accepted ones expire), pushes them to GHCR and attaches build provenance attestations (D-040).
- Deploy tooling: `deploy.sh` (pull, pre-deploy backup, migrate, start, smoke test; `versions.env` updated only after the smoke test passes), `rollback.sh`, and a smoke test (`python -m scc_web.smoke`) run as a one-off container (D-040, RB-13).
- Backups: nightly `pg_dump` through restic to Cloudflare R2 with retention (7 daily, 4 weekly, 6 monthly), `restore.sh` for drills into a scratch database or a full replace, an hourly disk check, systemd timers and Docker log rotation (RB-11, RB-12, RB-06).
- Alerts: gateway `POST /notify` sends Telegram messages, with quiet codes and repeats held back for an hour; the gateway alerts when the budget cap refuses a call, core when a job fails; host scripts alert directly (RB-09).
- `tools/rehearse_deploy.py` rehearses deploy, rollback, backup and restore on Docker Desktop with production's compose file.

### Changed
- Python images no longer ship pip, and the postgres image installs Debian security updates at build time (P1.11).
- edge uses cloudflared 2026.10.3, taken before it was two weeks old for a certificate-validation fix (D-040).

### Fixed
- Release workflow: stays logged in to GHCR until the build provenance attestation is stored.
