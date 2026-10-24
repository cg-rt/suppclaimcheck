# Changelog

One version for the whole app (D-034). Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - not yet released

### Added
- Walking-skeleton modules: `edge` (Cloudflare Tunnel), `web`, `core` (`core-api` and a placeholder `core-worker`), `gateway`, `postgres` (PostgreSQL 18 + pgvector). Each has a Dockerfile with a base image pinned by digest and a non-root user.
- `/health`, `/health/deps` and `/version` on every HTTP module; JSON logs with a correlation ID passed between modules (`X-Correlation-ID`).
- Development and production compose files: an internal network with no internet, egress only for `edge` and `gateway`, Docker secrets, no host ports in production.
- One database schema and least-privilege role per module, with per-module Alembic migrations run as a one-off step; `auth.users` table.
