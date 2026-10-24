# deploy

Everything production runs. The VM keeps no source code: `/opt/suppclaimcheck` holds the files below, the secret files and the Docker volumes. Images come from GHCR, built and scanned by the release workflow (P1.11). The owner runs every command here (AGENTS.md section 3).

| File | Does |
|---|---|
| `compose.prod.yaml` | The production stack (P1.02) |
| `versions.env` | The app version running, and the one before it (D-034). `deploy.sh` rewrites it on the VM |
| `deploy.sh`, `rollback.sh` | Deploy a version; return to the last good one (P1.12, RB-13) |
| `backup.sh`, `restore.sh` | Database backup to R2 with restic; restore drill or real restore (P1.13, RB-11, RB-12) |
| `disk-check.sh` | Alerts when the disk passes 80% (P1.13, RB-06) |
| `notify.sh` | Sends a Telegram message from the VM; used by the scripts above (P1.14) |
| `systemd/` | Timers: nightly backup at 03:30, hourly disk check |
| `host/` | Builds the VM from nothing ([host/README.md](host/README.md)); `04-ops-timers.sh` installs the timers |
| `compose.dev.yaml` | The development stack on Docker Desktop (not used on the VM) |

## Releases

1. Merge the release PR, which sets `VERSION` and the `CHANGELOG.md` heading.
2. Tag the merge commit `vX.Y.Z` and push the tag. The **Release** workflow checks the tag matches `VERSION`, builds all five images, scans them with Trivy (fixable critical or high findings fail it), pushes them to `ghcr.io/cg-rt/suppclaimcheck-<module>:X.Y.Z` and attaches a build provenance attestation.
3. Check an image's attestation from any machine with the GitHub CLI:
   ```bash
   gh attestation verify oci://ghcr.io/cg-rt/suppclaimcheck-web:0.1.0 --owner cg-rt
   ```

Accepted scan findings live in `.trivyignore.yaml`, each with a reason and an expiry date, after which the release fails again until it is dealt with.

## First-time setup on the VM

After `host/03-docker-firewall-updates.sh`:

1. **Make the GHCR packages public** (once, after the first release): GitHub → your profile → Packages → each `suppclaimcheck-*` package → Package settings → Change visibility → Public. The code is public already; public images mean the VM needs no registry login.
2. **Copy the files** from a checkout on Windows (PowerShell, in the repo folder):
   ```powershell
   scp deploy/compose.prod.yaml deploy/versions.env deploy/*.sh deploy/host/04-ops-timers.sh scc-prod:/tmp/
   scp -r deploy/systemd scc-prod:/tmp/
   ```
   On the VM:
   ```bash
   sudo mkdir -p /opt/suppclaimcheck/secrets && sudo mv /tmp/04-ops-timers.sh ~/ && sudo mv /tmp/compose.prod.yaml /tmp/versions.env /tmp/*.sh /tmp/systemd /opt/suppclaimcheck/
   ```
   ```bash
   sudo chmod 0750 /opt/suppclaimcheck/*.sh
   ```
3. **Secret files** in `/opt/suppclaimcheck/secrets/`, one value per file, folder `0700` root, files `0444` (header of `compose.prod.yaml`). Values come from the password manager; type them with `sudo nano`, never on the command line.

   | File | What |
   |---|---|
   | `postgres_superuser_password`, `db_web_password`, `db_core_password`, `db_gateway_password` | New random passwords, made on the VM: `openssl rand -base64 32` |
   | `smoke_password` | The smoke account's password; `deploy.sh` creates the account `smoke@suppclaimcheck.com` from it |
   | `anthropic_api_key`, `ncbi_api_key` | API keys (the gateway only) |
   | `tunnel_token` | Cloudflare Tunnel token (C1.2) |
   | `telegram_bot_token`, `telegram_chat_id` | Alerts (P1.14) |
   | `restic.env` | Three lines: `RESTIC_REPOSITORY=s3:https://<account-id>.r2.cloudflarestorage.com/scc-backups`, `AWS_ACCESS_KEY_ID=...`, `AWS_SECRET_ACCESS_KEY=...` (the R2 token) |
   | `restic_password` | The backup encryption password. **Without it the backups can't be read: keep a copy in the password manager.** |
   | `healthchecks_backup_url`, `healthchecks_disk_url` | Ping URLs of the two healthchecks.io checks (C1.7) |

4. **First deploy** (C1.3):
   ```bash
   cd /opt/suppclaimcheck && sudo ./deploy.sh 0.1.0
   ```
   Then create your own admin account:
   ```bash
   sudo docker compose -f compose.prod.yaml --env-file versions.env exec web python -m scc_web.admin create-user you@example.com --admin
   ```
5. **Timers and log rotation:**
   ```bash
   sudo bash ~/04-ops-timers.sh
   ```

## Rehearsing on Docker Desktop

`uv run python tools/rehearse_deploy.py` prepares `.rehearsal/` (git-ignored) with these scripts, production's compose file, local images tagged as three versions, and a local restic repository. RB-13 lists the commands.
