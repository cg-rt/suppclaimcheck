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
**Status:** draft, verify in P1.15
- **Symptoms:** the disk alert (80%) fires; containers fail to start; backups fail.
- **Commands:**
  ```bash
  df -h /
  docker system df
  sudo du -sh /var/lib/docker/* 2>/dev/null | sort -h | tail
  ```
- **Fix:** remove unused images: `docker image prune -a --filter "until=168h"`. Older versions can be pulled again from GHCR for a rollback. Check that Docker log rotation is on. If it's still tight, grow the VM disk: shut the VM down, resize the VHDX in Hyper-V Manager, start it, then grow the partition and filesystem in Ubuntu.
- **Verify:** free space is above 20%; all services healthy.

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
**Status:** refusal verified in dev 2026-10-24 (gateway test `test_cap_reached_refuses_before_the_provider_is_called`, and a dev job with the cap lowered to EUR 0.0001). The Telegram alert (P1.14) and the admin banner (P3.04) are not built yet.
- **Symptoms:** jobs fail with "This month's budget for checks is used up. Checks will work again next month."; the gateway logs `budget cap reached` with the lane, the spend and the cap.
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
- **Verify:** the spend query shows the expected total; new jobs are accepted or refused as intended.

## RB-10 Suspected abuse or injection attempts
**Status:** draft, verify in P3
- **Symptoms:** injection or abuse flags in admin; one account using its quota repeatedly; strange inputs.
- **Fix:** review the flagged jobs in admin; disable the account if needed; tighten its quota; add a Cloudflare WAF rule if the traffic is external. Record it as an incident.
- **Verify:** no new flags from that account; costs back to normal.

## RB-11 Backup failed
**Status:** draft, verify in P1.15
- **Symptoms:** healthchecks.io or Telegram alert: no backup ping.
- **Commands:**
  ```bash
  systemctl status scc-backup.service
  journalctl -u scc-backup.service --since today
  ```
- **Fix:** the usual causes are expired R2 credentials, a full disk (RB-06), or the database being down (RB-04). Fix the cause, then run `sudo systemctl start scc-backup.service`.
- **Verify:** the restic snapshot list shows today's snapshot; healthchecks.io is green.

## RB-12 Restore from backup
**Status:** draft, verify in C1.5
- **When:** data loss, corruption, or a restore drill (monthly).
- **Steps:** run `./deploy/restore.sh <snapshot-id> <target-db>`, restoring into a scratch database first. Compare row counts with production. For a real restore: stop `core-worker`, `core-api` and `web`, restore into `scc`, start them again, and run the smoke test.
- **Verify:** row counts match the snapshot; smoke test passes; record it in the private log.

## RB-13 Deploy and roll back
**Status:** draft, verify in P1.12
- **Deploy:** `./deploy/deploy.sh <version>` pulls the images, runs migrations, restarts and runs the smoke test.
- **Roll back:** `./deploy/rollback.sh` returns the stack to the previous version in `versions.env`. Migrations must be backward-compatible for one version, so a rollback never needs a down-migration.
- **Verify:** `/version` shows the expected version; smoke test passes.

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
