#!/usr/bin/env bash
# Disk-space check (P1.13, RB-06): alerts on Telegram when a filesystem passes the threshold. Run hourly by
# the scc-disk-check timer as root. Pings healthchecks.io (success, or /fail when over) when
# secrets/healthchecks_disk_url exists, so a check that stops running is noticed too.
set -uo pipefail

dir="${SCC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
threshold="${SCC_DISK_ALERT_PERCENT:-80}"
hc_url="$(tr -d '[:space:]' 2> /dev/null < "$dir/secrets/healthchecks_disk_url" || true)"

# The root filesystem and Docker's data folder (often the same one).
report="$(df --output=pcent,target / /var/lib/docker 2> /dev/null | tail -n +2 | sort -u)"
over="$(echo "$report" | awk -v t="$threshold" '{ gsub("%", "", $1); if ($1 + 0 >= t) print $2 " at " $1 "%" }')"

if [ -n "$over" ]; then
  echo "disk over ${threshold}%: $over"
  "$dir/notify.sh" "Disk over ${threshold}%: $over. See RB-06." || true
  if [ -n "$hc_url" ]; then curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "$over" "$hc_url/fail" || true; fi
  exit 1
fi
echo "disk ok: $(echo "$report" | tr '\n' ' ')"
if [ -n "$hc_url" ]; then curl -fsS -m 10 --retry 3 -o /dev/null "$hc_url" || true; fi
