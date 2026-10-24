# Changelog

One version for the whole app (D-034). Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Jobs API in core-api (`POST /jobs` with an `Idempotency-Key`, `GET /jobs/{id}`, `POST /jobs/{id}/confirm`) and a Procrastinate worker in core-worker; jobs carry their correlation ID.
- Pipeline v0: claim extraction, confirmation, PubMed query by code, one open-access article from Europe PMC, JATS passages ranked by Postgres full-text search, passage assessment, verbatim quote verification with hallucination events, and rubric v0 for the verdict and score.
- Gateway: schema-constrained Claude calls with a monthly cap per lane that fails closed, cost from a versioned price table, retries on transient errors only, and a record of every call; PubMed and Europe PMC with the licence allowlist, a rate limiter and an article cache.
- Committed OpenAPI specs for web, core and gateway, checked against the code in CI.
- Login in web: invite-only accounts from an admin CLI (`python -m scc_web.admin`), Argon2id hashes, server-side sessions in a `__Host-` cookie with idle and absolute timeouts, CSRF tokens on every form, sign-in lockout and rate limit, password change (D-039).
- Web pages: sign in, new check, confirm the claim, live progress over server-sent events, result (verdict, score with its arithmetic, verified quotes with citation and licence, what was searched, what could not be checked, not-medical-advice notice), history and account. Design tokens in `docs/DESIGN.md`.
- core-api: `GET /jobs?user_id=` (history) and `GET /jobs/{id}/events` (progress).
- Browser smoke test with axe accessibility checks (`tests/e2e`), run against a running stack.

## [0.1.0] - not yet released

### Added
- Walking-skeleton modules: `edge` (Cloudflare Tunnel), `web`, `core` (`core-api` and a placeholder `core-worker`), `gateway`, `postgres` (PostgreSQL 18 + pgvector). Each has a Dockerfile with a base image pinned by digest and a non-root user.
- `/health`, `/health/deps` and `/version` on every HTTP module; JSON logs with a correlation ID passed between modules (`X-Correlation-ID`).
- Development and production compose files: an internal network with no internet, egress only for `edge` and `gateway`, Docker secrets, no host ports in production.
- One database schema and least-privilege role per module, with per-module Alembic migrations run as a one-off step; `auth.users` table.
