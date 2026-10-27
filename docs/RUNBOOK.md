# RUNBOOK

What to do when something breaks. Linked from the admin dashboard (P3.10); admin failure types link to the `RB-nn` entries (D-035).
Every entry has: **Symptoms → Checks → Commands → Fix → Verify.** After any incident, add an entry to the owner's private log (what happened, cause, fix, downtime, what to change).

> **All entries are drafts written before the system exists.** Each one is marked verified only after it has been exercised (P1.15, P3.13). Do not trust an unverified command blindly.

Production commands run on the VM, in `/opt/suppclaimcheck`, unless stated otherwise. Connect from Windows with `ssh scc-prod`.
Service names: `edge`, `web`, `core-api`, `core-worker`, `gateway`, `postgres`.

---

## RB-01 Tunnel down
**Status:** draft, verify in P1.15
- **Symptoms:** the site doesn't load from outside; UptimeRobot alerts; Cloudflare shows error 1033 or 530; the Cloudflare dashboard shows the tunnel as inactive or degraded.
- **Checks:** Is the VM up (RB-02)? Is the `edge` container running? Does the VM have internet?
- **Commands:**
  ```bash
  docker compose ps edge
  docker compose logs --since 30m edge
  curl -sI https://www.cloudflare.com | head -1
  ```
- **Fix:** if `edge` is stopped or restarting, `docker compose up -d edge`. If the logs show an auth error, the tunnel token is wrong or was rotated: put the token from the Cloudflare dashboard (Zero Trust → Networks → Tunnels) into the secret file and restart `edge`. If the VM has no internet, check the Hyper-V Default Switch and the Windows network.
- **Verify:** the tunnel is healthy in the dashboard; the site loads on your phone over mobile data.

## RB-02 Machine rebooted by Windows Update
**Status:** VM part verified by the reboot drill on 2026-10-23: Windows booted, and the VM started on its own about a minute later (the 30 s start delay plus boot), before anyone signed in. Containers to be verified in Phase 1.
- **Symptoms:** a short outage (UptimeRobot down, then up); Windows shows a recent restart.
- **Checks:** did the VM start by itself? Did all containers come back healthy?
- **Good to know:**
  - Windows may sign the user in automatically after a restart and then lock the screen, so a session can exist before anyone touches the PC. The VM's start does not depend on it.
  - The Default Switch gives the VM a new IP address after a host restart. Always use `scc-prod.mshome.net` (or the `scc-prod` SSH alias), never the IP.
  - The VM's clock runs on UTC; compare times with the host's local time accordingly.
- **Commands** (Windows PowerShell as administrator, then the VM):
  ```powershell
  Get-VM scc-prod | Select-Object Name, State, Uptime
  ```
  ```bash
  uptime -s
  docker compose ps
  ```
- **Fix:** if the VM is Off, run `Start-VM scc-prod` and check its automatic start action in Hyper-V Manager. If containers are not running, `docker compose up -d`, and check that `docker` is enabled (`sudo systemctl is-enabled docker`).
- **Verify:** all services healthy; a test claim completes. Log the downtime from UptimeRobot.

## RB-03 Container stuck in a restart loop
**Status:** draft, verify in P1.15
- **Symptoms:** `docker compose ps` shows `Restarting`; health checks failing; alerts.
- **Commands:**
  ```bash
  docker compose ps
  docker inspect -f '{{.RestartCount}}' $(docker compose ps -q <service>)
  docker compose logs --tail 100 <service>
  ```
- **Fix:** the usual causes are a bad config or secret, a failed migration, or the database being unreachable. If it started after a deploy, roll back (RB-13). If it's a secret, fix the secret file and `docker compose up -d <service>`.
- **Verify:** the service is healthy and stays up for 10 minutes.

## RB-04 Database down
**Status:** draft, verify in C3.2
- **Symptoms:** web shows the maintenance or error page; core-api and gateway health checks fail; job processing stops. The admin log view is empty, because logs only reach `docker compose logs` while the database is down.
- **Commands:**
  ```bash
  docker compose ps postgres
  docker compose logs --tail 200 postgres
  df -h /
  ```
- **Fix:** if the disk is full, go to RB-06 first. Otherwise `docker compose up -d postgres` and wait for it to be healthy. If the data directory is corrupted, restore from backup (RB-12).
- **Verify:** all services healthy; jobs that were in progress either resume or show as failed, with a manual retry available in admin.

## RB-05 Database full
**Status:** draft, verify in P3.13
- **Symptoms:** write errors in the logs ("could not extend file", "no space left on device"); the disk alert fires.
- **Commands:**
  ```bash
  docker compose exec postgres psql -U postgres -d scc -c "select pg_size_pretty(pg_database_size('scc'));"
  docker compose exec postgres psql -U postgres -d scc -c "select relname, pg_size_pretty(pg_total_relation_size(relid)) from pg_catalog.pg_statio_user_tables order by pg_total_relation_size(relid) desc limit 10;"
  ```
- **Fix:** run the retention purge (logs older than 30 days, expired screenshots, old cached articles). If the data really is large, grow the disk (RB-06).
- **Verify:** free space is above 20%; writes succeed.

## RB-06 Disk full
**Status:** the alert is rehearsed in dev on 2026-10-24 (`disk-check.sh` with the threshold lowered to 1% ran the alert path); the fix steps are a draft, verify in P3.13.
- **Symptoms:** a Telegram alert "Disk over 80%" from the hourly `scc-disk-check` timer (or healthchecks.io reports the disk check as failing); containers fail to start; backups fail.
- **Commands:**
  ```bash
  df -h /
  docker system df
  sudo du -sh /var/lib/docker/* 2>/dev/null | sort -h | tail
  journalctl -u scc-disk-check --since today --no-pager
  ```
- **Fix:** remove unused images: `docker image prune -a --filter "until=168h"`. Older versions can be pulled again from GHCR for a rollback. Check that Docker log rotation is on (`cat /etc/docker/daemon.json`; `04-ops-timers.sh` writes it). If it's still tight, grow the VM disk: shut the VM down, resize the VHDX in Hyper-V Manager, start it, then grow the partition and filesystem in Ubuntu.
- **Verify:** free space is above 20%; `sudo ./disk-check.sh` prints `disk ok`; all services healthy.

## RB-07 PubMed or full-text source down
**Status:** draft, verify in P2
- **Symptoms:** jobs fail or stall at `searching` or `retrieving`; the gateway logs show timeouts, 5xx or 429 from E-utilities or Europe PMC.
- **Commands:**
  ```bash
  docker compose logs --since 30m gateway | grep -Ei "eutils|europepmc|timeout|429|5[0-9]{2}"
  ```
- **Fix:** for 5xx or timeouts, the service is down; retries with backoff run automatically and jobs fail cleanly once retries are exhausted. Retry them from admin later. For 429, check that the NCBI API key is set and the limiter is at 8 requests/s or lower. If Europe PMC is down but PubMed works, jobs fail at `retrieving` with the message "The literature databases are not answering": the PMC Cloud Service fallback (D-017) is not built yet. Articles already in the cache (`gateway.articles`) are still served.
- **Verify:** a test claim completes; failed jobs retried.

## RB-08 LLM provider errors or rate limits
**Status:** draft, verify in C3.1
- **Symptoms:** jobs fail at `extracting` or `assessing`; gateway errors with 401, 429 or 5xx; cost stops increasing.
- **Commands:**
  ```bash
  docker compose logs --since 30m gateway | grep -Ei "provider|401|429|5[0-9]{2}"
  ```
  The job's `error` and the gateway's `llm_calls.error` name the cause: `provider_unavailable` (gave up after retries), `provider_rejected` (4xx, often the key), `provider_not_configured` (no key in the `anthropic_api_key` secret), `provider_refused` (the model declined).
- **Fix:** 401 means the key is wrong, expired or revoked: rotate the secret and restart `gateway`. 429 means rate-limited: backoff handles short bursts; for sustained limits, check the provider console limits. 5xx means a provider outage: check the provider's status page. v1 has a single provider (D-033), so there is no fallback: jobs fail cleanly at that stage; retry them from admin once the status page shows recovery.
- **Verify:** a test claim completes; failed jobs retried from admin.

## RB-09 Budget cap reached
**Status:** rehearsed in dev on 2026-10-24: with the cap lowered to EUR 0.0001, a job failed with the budget message, the gateway logged `budget cap reached` and raised the `budget_cap_reached` alert (Telegram not configured in dev, so it logged `alert not sent`). Alert delivery is covered by `services/gateway/tests/test_alerts.py`; a real phone message comes with C1.3. The admin banner arrives with P3.04.
- **Symptoms:** a Telegram alert "budget_cap_reached · live" (sent once an hour at most); jobs fail with "This month's budget for checks is used up. Checks will work again next month."; the gateway logs `budget cap reached` with the lane, the spend and the cap.
- **How the cap works:** before every LLM call, the gateway reserves the call's worst-case cost and refuses it if this month's spend plus that would pass the lane's cap (`live` EUR 15, `eval` EUR 5, in `services/gateway/src/scc_gateway/config/gateway-v0.toml`). If the database can't be reached, no call is made.
- **Checks:** this month's spend per lane, from the VM:
  ```bash
  docker compose exec postgres psql -U postgres -d scc -c "SELECT lane, status, count(*), sum(cost_eur) FROM gateway.llm_calls WHERE created_at >= date_trunc('month', now()) GROUP BY 1, 2"
  ```
  Is this normal use, or a spike from one user (RB-10)? Was there an eval run?
- **Fix:** if it's abuse, go to RB-10. If it's legitimate, either wait for the next month or, **the owner only**, raise the cap with a new config version and a reason. Never raise it past the EUR 25 total without a DECISIONS entry.
- **Drill (dev):** `SCC_BUDGET_CAP_OVERRIDE_EUR` lowers every cap, and can never raise one. Restart the gateway with it, submit a claim, and check the job fails with the budget message; then restart without it.
  ```bash
  SCC_BUDGET_CAP_OVERRIDE_EUR=0.0001 docker compose -f deploy/compose.dev.yaml up -d gateway
  ```
  ```bash
  docker compose -f deploy/compose.dev.yaml up -d gateway
  ```
- **Verify:** the spend query shows the expected total; new jobs are accepted or refused as intended.

## RB-10 Suspected abuse or injection attempts
**Status:** draft, verify in P3
- **Symptoms:** injection or abuse flags in admin; one account using its quota repeatedly; strange inputs.
- **Fix:** review the flagged jobs in admin; disable the account if needed; tighten its quota; add a Cloudflare WAF rule if the traffic is external. Record it as an incident.
- **Verify:** no new flags from that account; costs back to normal.

## RB-11 Backup failed
**Status:** rehearsed in dev on 2026-10-24 against a local restic repository: with the database stopped, `backup.sh` failed, kept no snapshot (3 before, 3 after) and raised the alert. Verify on the VM with C1.5 and C1.7.
- **Symptoms:** a Telegram alert "Backup (nightly) FAILED" with the last log lines, or healthchecks.io reports the backup check as late or failed.
- **Commands:**
  ```bash
  systemctl status scc-backup.service --no-pager
  journalctl -u scc-backup.service --since yesterday --no-pager
  ```
- **Fix:** the usual causes are expired R2 credentials (`secrets/restic.env`), a full disk (RB-06), the database being down (RB-04), or DNS not working on the VM: the alert then says the storage host did not resolve. `backup.sh` waits up to 10 minutes for DNS, because a backup missed while the host was off runs right at boot and the Hyper-V Default Switch can take minutes to answer DNS (seen 2026-10-26). Check with `resolvectl query cloudflare.com`. Fix the cause, then run the backup again:
  ```bash
  sudo systemctl start scc-backup.service
  ```
- **Verify:** the journal ends with `backup (nightly) done: <snapshot id> ...`; healthchecks.io is green.

## RB-12 Restore from backup
**Status:** rehearsed in dev on 2026-10-24: a scratch restore compared row counts (all equal except the one row added after the backup), and a full replace restored a pinned snapshot, reran the migrate steps and passed the smoke test. The VM drill is C1.5.
- **When:** data loss, corruption, or a restore drill (monthly).
- **Drill (no downtime):** restores the latest snapshot into a new scratch database and prints row counts, live vs restored, per table:
  ```bash
  sudo ./restore.sh
  ```
  A named snapshot instead: `sudo restic snapshots` (with `secrets/restic.env` loaded) lists them, then `sudo ./restore.sh <id>`. Drop the scratch database afterwards with the command it prints.
- **Real restore (downtime):** makes a safety backup of the current state, stops the app, replaces the `scc` database with the snapshot, then redeploys the recorded version (migrate steps and smoke test). It asks you to type `REPLACE`.
  ```bash
  sudo ./restore.sh latest --replace
  ```
- **Verify:** row counts look as expected; the smoke test passed; record it in the private log.

## RB-13 Deploy and roll back
**Status:** rehearsed in dev on 2026-10-24 with production's compose file and scripts (`tools/rehearse_deploy.py`): first deploy, upgrade, a deliberately failing deploy, rollback after the failure, rollback after a good deploy, and a deploy with the pre-deploy backup. The first VM deploy is C1.3.
- **Deploy:** pulls the version's images, backs up the database, runs the migrate steps, starts everything, then runs the smoke test (health, version, sign-in, history, and one canned claim, which costs about EUR 0.01 of the live budget). `versions.env` changes only after the smoke test passes.
  ```bash
  sudo ./deploy.sh 0.2.0
  ```
  Add `--no-claim` to skip the canned claim (no LLM calls).
- **When a deploy fails:** it prints what failed and alerts on Telegram. `versions.env` still names the last good version. Roll back:
  ```bash
  sudo ./rollback.sh
  ```
  After a failed deploy it returns to the recorded good version; after a good deploy, to the previous one; `sudo ./rollback.sh 0.1.0` goes to a version you name. A rollback skips the canned claim.
- **Migrations:** forward-only. A rollback runs the older code on the newer schema, which is safe while migrations only add tables and columns. A migration that removes or renames must ship in two releases (add, then remove), or the rollback becomes a restore (RB-12) from the pre-deploy backup.
- **Rehearse in dev:** `uv run python tools/rehearse_deploy.py`, then from `.rehearsal/` in Git Bash:
  ```bash
  SCC_PROJECT=scc-rehearsal SCC_COMPOSE_EXTRA=compose.rehearsal.yaml SCC_SKIP_PULL=1 ./deploy.sh 0.1.0 --no-claim
  ```
- **Verify:** the smoke test printed `smoke test passed`; `cat versions.env` shows the new version and the previous one.

---

## RB-14 Rebuild the production VM
**Status:** steps verified on 2026-10-23 (first build)
- **When:** the VM is lost or corrupted, or the host is replaced.
- **Steps:** follow [`deploy/host/README.md`](../deploy/host/README.md): turn on Hyper-V, create the VM with `01-create-vm.ps1`, install Ubuntu Server, set up key login, then run `02-ssh-hardening.sh` and `03-docker-firewall-updates.sh`. Then deploy the current versions (RB-13) and restore the database (RB-12).
- **Verify:** only SSH answers from the host; `docker run --rm hello-world` works on the VM; the smoke test passes after the deploy.

## Release go-live checklist
To be completed in P3.13. Copy it into the private log for each release and tick it there.
- [ ] CI green on the release commit; all tests pass
- [ ] Evals run on the release config; thresholds met (EVALS.md)
- [ ] Security checks: scans clean or triaged; no new secrets findings
- [ ] Backup taken today, and a restore verified within the last 30 days
- [ ] Runbook updated for anything new
- [ ] Rollback tested for the modules being released
- [ ] Feature flags set as intended for this rollout stage
- [ ] Owner's go-ahead recorded

## Weekly ops checklist
To be completed in P3.13. About 15 minutes, every Monday.
- [ ] UptimeRobot: any downtime last week? Log it if so
- [ ] healthchecks.io: all backups and disk checks green
- [ ] Disk usage below 70% (`df -h /`)
- [ ] Costs this month vs budget; any unusual user
- [ ] Admin: failed jobs, safety flags, injection flags reviewed
- [ ] Dependabot and security alerts triaged
- [ ] Ubuntu updates applied; reboot the VM if required (`/var/run/reboot-required`)
