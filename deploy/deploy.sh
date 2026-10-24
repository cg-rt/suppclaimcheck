#!/usr/bin/env bash
# Deploys one app version (P1.12, D-022, D-034). Run on the VM from /opt/suppclaimcheck, as root:
#   sudo ./deploy.sh 0.2.0              pull, back up, migrate and start, smoke test with one canned claim
#   sudo ./deploy.sh 0.2.0 --no-claim   the same, but the smoke test makes no LLM calls
# versions.env changes only after the smoke test passes, so a failed deploy leaves the last good version
# recorded and ./rollback.sh returns to it (RB-13). Schema migrations are forward-only: a rollback runs
# the older code on the newer schema, which is safe while migrations only add.
set -euo pipefail

dir="${SCC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$dir"
project="${SCC_PROJECT:-scc}"
compose=(docker compose --project-name "$project" -f compose.prod.yaml)
if [ -n "${SCC_COMPOSE_EXTRA:-}" ]; then compose+=(-f "$SCC_COMPOSE_EXTRA"); fi

usage() { echo "usage: $0 <version> [--no-claim] [--no-backup] [--rollback]" >&2; exit 2; }
[ $# -ge 1 ] || usage
target="$1"; shift
claim=1; backup=1; mode=deploy
for arg in "$@"; do
  case "$arg" in
    --no-claim) claim=0 ;;
    --no-backup) backup=0 ;;
    --rollback) mode=rollback; claim=0 ;;
    *) usage ;;
  esac
done
[[ "$target" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Not a version: $target (expected X.Y.Z)" >&2; exit 2; }

# shellcheck source=versions.env
. ./versions.env
current="${SCC_VERSION:-}"
previous="${SCC_PREVIOUS_VERSION:-}"

# One deploy at a time.
if command -v flock > /dev/null; then
  exec 9> .deploy.lock
  flock -n 9 || { echo "Another deploy is running." >&2; exit 1; }
fi

step() { printf '\n== %s\n' "$*"; }
fail() {
  echo "FAILED: $*" >&2
  echo "The stack may be partly on $target. Last good version in versions.env: ${current:-none}." >&2
  echo "Logs: docker compose --project-name $project -f compose.prod.yaml logs --tail 100" >&2
  echo "Return to the last good version: sudo ./rollback.sh   (RB-13)" >&2
  ./notify.sh "$mode to $target FAILED: $*. Last good: ${current:-none}. See RB-13." || true
  exit 1
}

step "$mode to $target (last good version: ${current:-none})"
export SCC_VERSION="$target"

if [ "${SCC_SKIP_PULL:-0}" != 1 ]; then
  step "pull images for $target"
  "${compose[@]}" pull --quiet || fail "could not pull the $target images"
fi

if [ "$backup" = 1 ] && [ -x ./backup.sh ] && [ -n "$("${compose[@]}" ps --quiet postgres 2> /dev/null)" ]; then
  step "back up the database before migrating"
  ./backup.sh pre-deploy || fail "the pre-deploy backup failed"
else
  step "no pre-deploy backup (none configured, no running database, or --no-backup)"
fi

step "migrate and start $target"
"${compose[@]}" up --detach --wait --wait-timeout 300 --remove-orphans || fail "the stack did not become healthy"

step "smoke test"
# The smoke account is a normal user; this creates it on the first deploy and keeps its password in step
# with the secret file afterwards.
"${compose[@]}" exec -T web python -m scc_web.admin ensure-user "${SCC_SMOKE_EMAIL:-smoke@suppclaimcheck.com}" \
  < secrets/smoke_password || fail "could not prepare the smoke account"
smoke=(python -m scc_web.smoke)
if [ "$claim" = 1 ]; then smoke+=(--claim); fi
"${compose[@]}" --profile smoke run --rm --no-deps smoke "${smoke[@]}" || fail "the smoke test failed"

# Recorded only now: the new version passed. Written to a temp file and moved, so it is never half-written.
if [ "$target" != "$current" ]; then previous="$current"; fi
cat > versions.env.tmp << EOF
# The one app version running in production (D-034), and the one before it for rollback.
# deploy.sh rewrites this file on the VM after a deploy passes its smoke test; never edit it by hand there.
SCC_VERSION=$target
SCC_PREVIOUS_VERSION=$previous
EOF
mv versions.env.tmp versions.env

step "done: $target is live (previous: ${previous:-none})"
./notify.sh "$mode to $target passed its smoke test (previous: ${previous:-none})." || true
