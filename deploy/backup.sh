#!/usr/bin/env bash
# Database backup (P1.13, D-024): pg_dump, encrypted by restic, stored in Cloudflare R2. Run on the VM as root:
#   sudo ./backup.sh nightly      what the scc-backup timer runs at night; then applies the retention policy
#   sudo ./backup.sh pre-deploy   what deploy.sh runs before migrating
# Needs secrets/restic.env (RESTIC_REPOSITORY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) and
# secrets/restic_password. Pings healthchecks.io when secrets/healthchecks_backup_url exists. A failure
# alerts on Telegram (RB-11). restic only keeps the snapshot if pg_dump finished without error.
set -euo pipefail

dir="${SCC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
cd "$dir"
label="${1:-nightly}"
case "$label" in nightly | pre-deploy | manual) ;; *) echo "usage: $0 [nightly|pre-deploy|manual]" >&2; exit 2 ;; esac
project="${SCC_PROJECT:-scc}"
compose=(docker compose --project-name "$project" -f compose.prod.yaml)
if [ -n "${SCC_COMPOSE_EXTRA:-}" ]; then compose+=(-f "$SCC_COMPOSE_EXTRA"); fi
# Any version tag will do for exec into the running postgres; compose only needs the variable set.
. ./versions.env
export SCC_VERSION="${SCC_VERSION:-0.0.0}"

hc_url="$(tr -d '[:space:]' 2> /dev/null < secrets/healthchecks_backup_url || true)"
ping_hc() { if [ -n "$hc_url" ]; then curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "${2:-}" "$hc_url$1" || true; fi; }

log="$(mktemp)"
trap 'rm -f "$log"' EXIT
on_error() {
  echo "backup ($label) FAILED" >&2
  tail -n 20 "$log" >&2
  ping_hc /fail "$(tail -n 20 "$log")"
  ./notify.sh "Backup ($label) FAILED. Last lines: $(tail -n 3 "$log" | tr '\n' ' '). See RB-11." || true
  exit 1
}
trap on_error ERR

if [ "$label" = nightly ]; then ping_hc /start; fi

# A syntax error in a sourced file ends bash without running the ERR trap, so check the file first.
# bash's own message quotes the bad line, which may hold a secret, so the alert gets a fixed text.
if ! bash -n secrets/restic.env 2> /dev/null; then
  echo "secrets/restic.env is missing or is not valid shell; check it with: sudo bash -n secrets/restic.env" >> "$log"
  on_error
fi
set -a
# shellcheck source=/dev/null
. secrets/restic.env
set +a
export RESTIC_PASSWORD_FILE="$dir/secrets/restic_password"

# Creates the restic repository on the very first run.
restic cat config > /dev/null 2>> "$log" || restic init >> "$log" 2>&1

restic backup --tag "$label" --host scc-prod --stdin-filename scc.dump \
  --stdin-from-command -- "${compose[@]}" exec -T postgres pg_dump -U postgres -d scc --format=custom \
  >> "$log" 2>&1

if [ "$label" = nightly ]; then
  # Retention from D-024: 7 daily, 4 weekly, 6 monthly. Pre-deploy snapshots: the last 5.
  restic forget --tag nightly --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune >> "$log" 2>&1
  restic forget --tag pre-deploy --keep-last 5 --prune >> "$log" 2>&1
fi

summary="$(restic snapshots --tag "$label" --latest 1 --compact 2>> "$log" | grep -E '^[0-9a-f]{8} ' | tail -n 1 || true)"
echo "backup ($label) done: $summary"
if [ "$label" = nightly ]; then ping_hc "" "$summary"; fi
exit 0
