#!/usr/bin/env bash
# Restores a database backup (P1.13, RB-12, drill C1.5). Run on the VM as root:
#   sudo ./restore.sh                  latest snapshot into a scratch database, then compare row counts (the drill)
#   sudo ./restore.sh <snapshot-id>    the same, from a chosen snapshot (`restic snapshots` lists them)
#   sudo ./restore.sh latest --replace replace the live database (RB-12): stops the app, asks you to type REPLACE
# The scratch database is left in place for you to inspect; drop it with the command printed at the end.
# Needs the database roles to exist, which they do on this VM (rebuilding the VM is RB-14).
set -euo pipefail

dir="${SCC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$dir"
snapshot="${1:-latest}"
mode="${2:-scratch}"
case "$mode" in scratch | --replace) ;; *) echo "usage: $0 [snapshot|latest] [--replace]" >&2; exit 2 ;; esac
project="${SCC_PROJECT:-scc}"
compose=(docker compose --project-name "$project" -f compose.prod.yaml)
if [ -n "${SCC_COMPOSE_EXTRA:-}" ]; then compose+=(-f "$SCC_COMPOSE_EXTRA"); fi
. ./versions.env
export SCC_VERSION="${SCC_VERSION:-0.0.0}"
set -a
# shellcheck source=/dev/null
. secrets/restic.env
set +a
export RESTIC_PASSWORD_FILE="$dir/secrets/restic_password"

psql() { "${compose[@]}" exec -T postgres psql -U postgres -v ON_ERROR_STOP=1 -X -q "$@"; }

# Exact row counts for every table in the app schemas, as "schema.table count" lines, sorted.
counts() {
  psql -d "$1" -At -c "
    SELECT format('SELECT %L || '' '' || count(*) FROM %I.%I;', n.nspname || '.' || c.relname, n.nspname, c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r' AND n.nspname IN ('auth', 'core', 'gateway') ORDER BY 1" \
    | psql -d "$1" -At | LC_ALL=C sort
}

restic snapshots "$snapshot" --compact
# Pin "latest" to an ID now: --replace makes a safety backup first, which would otherwise become "latest".
snapshot="$(restic snapshots "$snapshot" --compact | grep -E '^[0-9a-f]{8} ' | tail -n 1 | cut -d' ' -f1)"
[ -n "$snapshot" ] || { echo "No such snapshot." >&2; exit 1; }
if [ "$mode" = --replace ]; then
  target=scc
  echo "This replaces the live database 'scc' with snapshot $snapshot. The site is down until it finishes."
  read -r -p "Type REPLACE to go on: " answer
  [ "$answer" = REPLACE ] || { echo "Stopped; nothing changed."; exit 1; }
  ./backup.sh manual   # the current state, in case the restore was the wrong call
  "${compose[@]}" stop edge web core-api core-worker gateway
  psql -d postgres -c "DROP DATABASE scc WITH (FORCE)" -c "CREATE DATABASE scc"
else
  target="scc_restore_$(date +%Y%m%d_%H%M%S)"
  psql -d postgres -c "CREATE DATABASE $target"
fi

echo "Restoring $snapshot into $target ..."
restic dump "$snapshot" scc.dump | "${compose[@]}" exec -T postgres pg_restore -U postgres -d "$target" --exit-on-error

if [ "$mode" = --replace ]; then
  # A deploy of the recorded version reruns the migrate steps (roles and grants) and the smoke test.
  ./deploy.sh "$SCC_VERSION" --no-claim --no-backup
  echo "Live database restored from $snapshot."
  exit 0
fi

echo
echo "Row counts: live database vs restored copy (differences are expected for rows added since the backup)"
LC_ALL=C join -a1 -a2 -e 0 -o 0,1.2,2.2 <(counts scc) <(counts "$target") | awk '{ printf "%-36s %8s %8s%s\n", $1, $2, $3, ($2 == $3 ? "" : "  differs") }'
echo
echo "Drop the scratch copy when done: sudo docker compose --project-name $project -f compose.prod.yaml exec postgres dropdb -U postgres $target"
