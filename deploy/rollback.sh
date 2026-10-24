#!/usr/bin/env bash
# Returns to the last good version (P1.12, RB-13). Run on the VM from /opt/suppclaimcheck, as root:
#   sudo ./rollback.sh            after a failed deploy: back to SCC_VERSION in versions.env (the last good one)
#                                 after a good deploy: back to SCC_PREVIOUS_VERSION
#   sudo ./rollback.sh 0.1.0      to a version you name
# It is a deploy of the older version (D-022), with the smoke test but no canned claim.
set -euo pipefail

dir="${SCC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$dir"
project="${SCC_PROJECT:-scc}"
compose=(docker compose --project-name "$project" -f compose.prod.yaml)
if [ -n "${SCC_COMPOSE_EXTRA:-}" ]; then compose+=(-f "$SCC_COMPOSE_EXTRA"); fi

# shellcheck source=versions.env
. ./versions.env

if [ $# -ge 1 ]; then
  target="$1"
else
  web="$(SCC_VERSION="$SCC_VERSION" "${compose[@]}" ps --quiet web 2> /dev/null || true)"
  running=""
  if [ -n "$web" ]; then running="$(docker inspect --format '{{.Config.Image}}' "$web" | sed 's/.*://')"; fi
  if [ -n "$running" ] && [ "$running" != "$SCC_VERSION" ]; then
    target="$SCC_VERSION"   # a deploy failed part-way: go back to the version recorded as good
  else
    target="${SCC_PREVIOUS_VERSION:-}"
  fi
  echo "Running: ${running:-unknown}. Recorded good: $SCC_VERSION. Previous: ${SCC_PREVIOUS_VERSION:-none}."
fi

[ -n "$target" ] || { echo "No version to roll back to: versions.env has no previous version." >&2; exit 1; }
exec ./deploy.sh "$target" --rollback
