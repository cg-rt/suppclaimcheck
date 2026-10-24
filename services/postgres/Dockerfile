# postgres: PostgreSQL 18 with pgvector (P1.01, P1.03). The official pgvector image, pinned by digest.
# The entrypoint starts as root only to fix data-folder ownership, then runs the server as the postgres user.
FROM pgvector/pgvector:0.8.7-pg18-trixie@sha256:9d9c930220cb9bf2f956d10a8f909cf9973d9672ca0278aef2c1e6facccad2e0
# Native health check.
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=10     CMD ["pg_isready", "-U", "postgres", "-d", "scc"]
