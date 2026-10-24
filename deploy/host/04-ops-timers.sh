#!/usr/bin/env bash
# Backups, disk checks and log rotation on the VM (P1.13). Run once as root, after /opt/suppclaimcheck holds
# the deploy files (see deploy/README.md):  sudo bash 04-ops-timers.sh
# Safe to run again. It changes nothing it can't check first, and never restarts Docker by itself.
set -euo pipefail

app=/opt/suppclaimcheck
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
for file in backup.sh restore.sh disk-check.sh notify.sh systemd/scc-backup.service systemd/scc-backup.timer \
  systemd/scc-disk-check.service systemd/scc-disk-check.timer; do
  [ -e "$app/$file" ] || { echo "Missing $app/$file: copy the deploy folder first." >&2; exit 1; }
done

echo "== restic"
command -v restic > /dev/null || { apt-get update -q && apt-get install -y -q restic; }
restic version
# --stdin-from-command, which backup.sh relies on, arrived in restic 0.17.
restic version | awk '{ split($2, v, "."); if (v[1] == 0 && v[2] < 17) { print "restic 0.17 or newer is needed"; exit 1 } }'

echo "== secret files for backups"
missing=0
for name in restic.env restic_password; do
  if [ ! -s "$app/secrets/$name" ]; then echo "  missing: $app/secrets/$name"; missing=1; fi
done
for name in healthchecks_backup_url healthchecks_disk_url telegram_bot_token telegram_chat_id; do
  [ -s "$app/secrets/$name" ] || echo "  optional, not set yet: $app/secrets/$name"
done
[ "$missing" = 0 ] || { echo "Create the missing files (deploy/README.md, Backups), then run this again." >&2; exit 1; }
chmod 0700 "$app/secrets"
chmod 0400 "$app/secrets/restic.env" "$app/secrets/restic_password"

echo "== scripts"
chmod 0750 "$app"/{backup,restore,disk-check,notify,deploy,rollback}.sh

echo "== systemd timers"
install -m 0644 "$app"/systemd/scc-*.service "$app"/systemd/scc-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now scc-backup.timer scc-disk-check.timer
systemctl list-timers 'scc-*' --no-pager

echo "== Docker log rotation"
# compose.prod.yaml already rotates every service's logs; this default also covers one-off containers.
if [ -e /etc/docker/daemon.json ]; then
  echo "  /etc/docker/daemon.json exists; left as it is:"
  cat /etc/docker/daemon.json
else
  printf '{\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "10m", "max-file": "5"}\n}\n' > /etc/docker/daemon.json
  echo "  wrote /etc/docker/daemon.json. It takes effect at the next Docker restart (sudo systemctl restart docker,"
  echo "  which restarts the containers too), or at the next reboot."
fi

echo
echo "Done. Test now: sudo systemctl start scc-backup && journalctl -u scc-backup -n 20 --no-pager"
