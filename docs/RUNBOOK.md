# RUNBOOK

What to do when something breaks. Linked from the admin dashboard (P3.10); admin failure types link to the `RB-nn` entries (D-035).
Every entry has: **Symptoms → Checks → Commands → Fix → Verify.** After any incident, add an entry to the owner's private log (what happened, cause, fix, downtime, what to change).
Every error code, what it means for the user and which entry handles it: [ERRORS.md](ERRORS.md).

> **Each entry's status says where it was exercised:** on the VM, rehearsed in dev, or only in tests (P1.15, P3.13, last reviewed 2026-11-06). Treat a step marked draft with care: run its check commands before its fix commands.

**Two health endpoints.** `/health` says a process is up; Docker's health checks use it, so a container stays "healthy" while the database is down. `/health/deps` also checks the database and the next module in the chain (web → core-api → gateway) and answers 503 with `"status":"failed"` when anything below it is down. `https://suppclaimcheck.com/health/deps` answers from anywhere and shows only ok or failed per dependency.

Production commands run on the VM, in `/opt/suppclaimcheck`, unless stated otherwise. Connect from Windows with `ssh scc-prod`.
Service names: `edge`, `web`, `core-api`, `core-worker`, `gateway`, `postgres`.

---

## RB-01 Tunnel down
**Status:** the symptom was seen on 2026-10-25, before the first deploy: with no connector running, the site answered Cloudflare error 1033 (HTTP 530). The fix steps are a draft, never needed so far; dev has no tunnel to rehearse on.
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
**Status:** rehearsed in dev on 2026-11-06: core-api was started with a database user that doesn't exist. It is a slow loop: each start waits about 30 seconds for the database, then logs `Application startup failed. Exiting.` and Docker starts it again (2 restarts in 75 seconds). Meanwhile web's `/health/deps` answered 503. Starting it again with the right settings brought it back healthy in 15 seconds. Not yet seen on the VM.
- **Symptoms:** `docker compose ps` shows the service as `Up 20 seconds (health: starting)` again and again, rarely as `Restarting`, because each attempt runs for a while; the restart count keeps growing. `/health/deps` answers 503; checks fail or never start.
- **Commands:**
  ```bash
  docker compose ps
  docker inspect -f '{{.RestartCount}}' $(docker compose ps -q <service>)
  docker compose logs --tail 100 <service>
  ```
- **Fix:** the usual causes are a bad config or secret, a failed migration, or the database being unreachable. `password authentication failed` in the logs means the service's database password secret doesn't match its role. If it started after a deploy, roll back (RB-13). If it's a secret, fix the secret file and `docker compose up -d <service>`.
- **Up but stuck:** Docker restarts a container that exits, never one that is only unhealthy. Up to 0.1.2, core-worker could hang after a database outage: up, `(unhealthy)`, logging nothing, running no checks (found in the RB-04 rehearsal; fixed in the next release, which exits instead). If `docker compose ps` shows any service `(unhealthy)` for more than a minute, look at its logs, then `docker compose restart <service>`.
- **Checks that lost their worker:** when core-worker restarts mid-check, the check stops moving. Every five minutes the worker's sweep ends any check stuck in a working state for more than `stall_minutes` (20, pipeline config) as failed with `stalled`, and a Telegram alert goes out. Once the worker is healthy, open each one in admin and press Retry: steps whose LLM calls already succeeded are not charged again (idempotency keys, D-053).
- **Verify:** the service is healthy and stays up for 10 minutes.

## RB-04 Database down
**Status:** rehearsed in dev on 2026-11-06 by stopping postgres for 90 seconds; the owner's drill on the VM is C3.2. What happened:
- The sign-in page still loaded, but signing in showed "Something went wrong" with a reference.
- `/health` stayed 200 on every module, so Docker kept web, core-api and gateway "healthy".
- `/health/deps` answered 503 with `"database":"failed"` on all three.
- core-worker stopped and was restarted by Docker.
- Five seconds after postgres started again, web, core-api and the gateway answered normally, with no restart.
- core-worker did not recover: it hung, unhealthy, until restarted by hand. That bug is fixed in the release after 0.1.2.
- **Symptoms:** signing in or any signed-in page shows "Something went wrong" with a reference; admin doesn't load; no check moves. UptimeRobot only notices if it watches `/health/deps` (the go-live checklist sets that); on `/health` the site looks up.
- **Commands:**
  ```bash
  docker compose ps
  docker compose logs --tail 200 postgres
  df -h /
  ```
  ```bash
  curl -s https://suppclaimcheck.com/health/deps
  ```
- **Fix:** if the disk is full, go to RB-06 first. Otherwise `docker compose up -d postgres` and wait for it to be healthy. If the data directory is corrupted, restore from backup (RB-12).
- **After it is back:** `docker compose ps` must show every service healthy within a minute. On 0.1.2, core-worker stays `(unhealthy)`: `docker compose restart core-worker`.
- **Verify:** `/health/deps` answers `"status":"ok"`; you can sign in; a test claim completes. A check that was running either finishes or ends as failed (`stalled` after 20 minutes) with a retry in admin.

## RB-05 Database full
**Status:** the commands were run against the dev database on 2026-11-06 (28 MB in all; the largest table is `gateway.articles`, the full-text cache, at 13 MB). The database lives on the VM's disk, so "full" means the disk is full: the alert is RB-06's. Not yet seen on the VM.
- **Symptoms:** write errors in the logs ("could not extend file", "no space left on device"); the disk alert fires.
- **Commands:**
  ```bash
  sudo docker compose exec postgres psql -U postgres -d scc -c "select pg_size_pretty(pg_database_size('scc'));"
  ```
  ```bash
  sudo docker compose exec postgres psql -U postgres -d scc -c "select schemaname || '.' || relname as table, pg_size_pretty(pg_total_relation_size(relid)) as size from pg_catalog.pg_statio_user_tables order by pg_total_relation_size(relid) desc limit 10;"
  ```
- **What shrinks by itself:** the purges delete checks after 180 days, sign-in attempts after 30, stored model answers after 7 (RB-16 shows how to see that they ran). The article, passage and embedding caches (`gateway.articles`, `core.passages`, `core.embeddings`) are never purged: they grow with every new article read. Clearing them is not scripted; a check fetches a missing article again, at no LLM cost.
- **Fix:** if the purges stopped (RB-16), find out why. Otherwise free space on the disk (RB-06), or grow it.
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
**Status:** rehearsed in dev on 2026-11-04: a claim was extracted, the gateway was cut off from the internet, and the confirmed claim failed at `searching` 12 seconds later, after three attempts with backoff, as `literature_unavailable` with the message "The literature databases are not answering. Please try again later." Admin's Errors listed it. The failure alert was not sent: Telegram was unreachable too. With the network back, Retry the search from the job's admin page finished the check (Supported, five articles, EUR 0.003). Not yet seen on the VM.
- **Symptoms:** a Telegram alert for a failed job with `literature_unavailable`; jobs failing at `searching` or `retrieving` in admin's Errors; the gateway logs show timeouts, 5xx or 429 from E-utilities or Europe PMC. If the whole connection is down, the Telegram alert can't go out either; UptimeRobot's email about the site is then the signal (RB-01).
- **Commands:**
  ```bash
  docker compose logs --since 30m gateway | grep -Ei "eutils|europepmc|timeout|429|5[0-9]{2}"
  ```
  Lines with `transient failure, retrying` name the URL that failed; `/literature/search` or `/literature/article` answering 503 means the retries ran out.
- **Fix:** for 5xx or timeouts, the service is down; retries with backoff run automatically and jobs fail cleanly once retries are exhausted. For 429, check that the NCBI API key is set and the limiter is at 8 requests/s or lower. If Europe PMC is down but PubMed works, jobs fail at `retrieving` with the same message: the PMC Cloud Service fallback (D-017) is not built yet. Articles already in the cache (`gateway.articles`) are still served. Once the source answers again, open each failed job in admin and press **Retry the search** (it runs on the job's own config version and charges its LLM calls again).
- **Verify:** a test claim completes; each failed job retried shows a result.

## RB-08 LLM provider errors or rate limits
**Status:** the codes are covered by `services/gateway/tests/test_llm_provider.py` (a 4xx is `provider_rejected`, never retried) and the retry policy by `test_budget_retry.py`; not rehearsed in dev, where the gateway may send real alerts. The owner's drill is C3.1, below.
- **Drill (C3.1, on the VM):**
  1. Note the time.
  2. Put a wrong value in the key's secret file: `sudo nano secrets/anthropic_api_key`, change one character, and keep the original in the password manager.
  3. `sudo docker compose restart gateway`.
  4. Start a check on the site. It fails at "Reading your text" with `provider_rejected`, a Telegram alert arrives, and admin's Errors lists it.
  5. Put the right key back and `sudo docker compose restart gateway`.
  6. Retry the check from its admin page; it finishes.
  7. Write the incident record: times, what you saw, what to change.
- **Symptoms:** jobs fail at `extracting` or `assessing`; gateway errors with 401, 429 or 5xx; cost stops increasing.
- **Commands:**
  ```bash
  docker compose logs --since 30m gateway | grep -Ei "provider|401|429|5[0-9]{2}"
  ```
  The job's `error` and the gateway's `llm_calls.error` name the cause: `provider_unavailable` (gave up after retries), `provider_rejected` (4xx, often the key), `provider_not_configured` (no key in the `anthropic_api_key` secret), `provider_refused` (the model declined).
- **Fix:** 401 means the key is wrong, expired or revoked: rotate the secret and restart `gateway`. 429 means rate-limited: backoff handles short bursts; for sustained limits, check the provider console limits. 5xx means a provider outage: check the provider's status page. v1 has a single provider (D-033), so there is no fallback: jobs fail cleanly at that stage; retry them from admin once the status page shows recovery.
- **Verify:** a test claim completes; failed jobs retried from admin.

## RB-09 Budget warning, cap or daily quota
**Status:** the cap was rehearsed in dev on 2026-10-24 (a job failed with the budget message; the gateway logged `budget cap reached` and raised the alert). The warnings, the stop before a new check, the daily quota and the banner (P3.04, D-053) are covered by `tests/integration/test_budget_guard.py` and `test_resilience.py`. Rehearsed in dev on 2026-11-04: with `SCC_BUDGET_CAP_OVERRIDE_EUR=0.01`, `/budget/status` reported `cap` for both lanes and core-api refused a new check with 402 and the budget message; without it, both lanes were back to `ok`. Not yet seen on the VM.
- **Symptoms:**
  - A Telegram alert `budget_warning · live_warn_50` or `live_warn_80`: the lane passed 50% or 80% of its cap this month (once per level and month). Admin shows a banner on every page until the month ends.
  - New checks refused with "This month's budget for checks is used up. Checks will work again next month." (core-api 402), and the banner turns red: less than one check's worth is left (`new_check_reserve_eur`, EUR 0.05). A confirmation is refused the same way. A check already running can still fail with `budget_cap_reached` if it hits the cap itself (Telegram `budget_cap_reached`, once an hour at most).
  - A user reports "This account has used today's checks." (core-api 429, `daily_quota_reached`): the account started `checks_per_user_per_day` checks (25) today, UTC.
- **How it works:** the gateway keeps the budget (D-021, D-053). Before every LLM call it reserves the worst case and refuses it past the lane's cap (`live` EUR 15, `eval` EUR 5). Before a new check or a confirmation, core-api asks the gateway's `/budget/status`; if the gateway can't answer, nothing starts (`budget_unavailable`). The daily quota counts each account's live extraction calls, one per check, whatever the outcome; the eval lane and `exempt_user_ids` are outside it. All of it is in the gateway config (`gateway-v1`).
- **Checks:** admin's Costs page: spend per lane against the cap, and this month's spend and today's checks per account. Or, on the VM:
  ```bash
  docker compose exec postgres psql -U postgres -d scc -c "SELECT lane, status, count(*), sum(cost_eur) FROM gateway.llm_calls WHERE created_at >= date_trunc('month', now()) GROUP BY 1, 2"
  ```
  Is this normal use, or a spike from one account (RB-10)? Was there an eval run?
- **Fix:** if it's abuse, go to RB-10. If it's legitimate, wait for the next month (the cap) or the next UTC day (the quota), or, **the owner only**, change the cap, the quota or `exempt_user_ids` with a new gateway config version and a reason (RB-15), then restart the gateway. Never raise a cap past the EUR 25 total without a DECISIONS entry.
- **Drill (dev):** `SCC_BUDGET_CAP_OVERRIDE_EUR` lowers every cap, and can never raise one. Restart the gateway with it, try a new check (refused, with the message) and open admin (red banner); then restart without it.
  ```bash
  SCC_BUDGET_CAP_OVERRIDE_EUR=0.01 docker compose -f deploy/compose.dev.yaml up -d gateway
  ```
  ```bash
  docker compose -f deploy/compose.dev.yaml up -d gateway
  ```
- **Verify:** the banner and the spend match what you expect; new checks are accepted or refused as intended.

## RB-10 Suspected abuse or injection attempts
**Status:** draft; the throttle and the alerts are covered by `tests/integration/test_guardrails.py` (P3.02, D-050), not yet exercised on the VM.
- **Symptoms:** a Telegram alert `injection_throttled` naming a user ID; a user reports "Too many checks from this account contained instructions to the system"; many refused checks from one account; strange inputs.
- **Checks:** admin's **Injections** page lists each account whose own text contained instructions, how many checks count in the window, and until when the throttle holds it, then every event with its job. Each job's trace shows its events and the text. Or from core's database:
  ```bash
  sudo docker compose exec postgres psql -U postgres -d scc -c "SELECT e.at, e.stage, e.source, j.state FROM core.injection_events e JOIN core.jobs j ON j.id = e.job_id WHERE e.user_id = <id> ORDER BY e.at DESC LIMIT 20"
  ```
  `source = submitted_text` counts against the user; an article ID (`PMC...`) means the instructions were in a fetched article, which never counts against anyone.
- **Fix:** the throttle lifts by itself 24 hours after the oldest counted check (from pipeline-v5, `guardrails`; admin's Injections page shows until when). If the account is abusive, disable it (`python -m scc_web.admin disable <email>`). Instructions in an article: nothing to do for the user; if one article keeps triggering it, note its PMCID in the incident record. External traffic: add a Cloudflare WAF rule. Record it as an incident.
- **Verify:** no new events from that account; costs back to normal.

## RB-11 Backup failed
**Status:** rehearsed in dev on 2026-10-24 against a local restic repository: with the database stopped, `backup.sh` failed, kept no snapshot (3 before, 3 after) and raised the alert. Seen on the VM on 2026-10-26: a backup failed (DNS at boot) and both the Telegram alert and the healthchecks.io email reached the owner.
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
**Status:** rehearsed in dev on 2026-10-24: a scratch restore compared row counts (all equal except the one row added after the backup), and a full replace restored a pinned snapshot, reran the migrate steps and passed the smoke test. Verified on the VM on 2026-10-27 (drill C1.5): snapshot `212196ee` restored into a scratch database, row counts equal in all 16 tables.
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
- **After a real restore, apply deletions again (P3.06):** a snapshot brings back any account deleted after it was taken. In Telegram, find every `account_deleted` alert sent since the snapshot's time. Each one names an account number. Delete each of those accounts again from admin's Users page, Delete; their checks go with them. Note the account numbers in the private log; never their email addresses.
- **Verify:** row counts look as expected; the smoke test passed; deletions applied again; record it in the private log.

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

## RB-15 Change, activate or roll back a config version
**Status:** draft; the commands were exercised against the dev stack and in `tests/integration/test_config_store.py` (P2.09, D-048), not yet on the VM.
- **What it is:** each of `core` (pipeline: prompts, N, K, retrieval, rubric, model), `gateway` (prices, budget caps, timeouts) and `web` (sessions, sign-in limits) has its own config versions in the database, one active pointer and an audit trail. The versions in the repo are registered by every deploy. A deploy activates the release's default only when that version is new, so your activations and rollbacks survive later deploys of the same release.
- **Symptoms:** answers got worse, slower or dearer right after a config change; or you want to try a setting without a release.
- **Checks:** what is active, what changed, and who changed it:
  ```bash
  sudo docker compose exec core-api python -m scc_core.config list
  ```
  ```bash
  sudo docker compose exec core-api python -m scc_core.config history
  ```
  ```bash
  sudo docker compose exec core-api python -m scc_core.config diff pipeline-v4
  ```
  The active version has a `*`; pipeline versions show their latest attached eval. For the other modules use `gateway python -m scc_gateway.config` and `web python -m scc_web.config`.
- **Roll back (undo the last activation):**
  ```bash
  sudo docker compose exec core-api python -m scc_core.config rollback --by owner --reason "what went wrong"
  ```
  Core: the next check uses it; checks already running finish on their version. Gateway and web: `sudo docker compose restart gateway` (or `web`) to apply it.
- **Try a new setting:** export the active version, edit a copy on the dev PC, and give it a new name such as `pipeline-v4-shorter-prompt` (names like `pipeline-v5` belong to releases). Measure it in dev first:
  ```bash
  uv run python -m scc_core.eval run --mode live --config-file pipeline-v4-shorter-prompt.toml --notes "why"
  ```
  (with `SCC_EVAL_ADMIN_DSN` set, as in docs/EVALS.md "How to run"; the run file lands in `evals/runs/`). Copy the TOML and the run file to the VM, then create the version, attach the eval and activate it:
  ```bash
  sudo docker compose exec -T core-api python -m scc_core.config create --file - --by owner --reason "why" < pipeline-v4-shorter-prompt.toml
  ```
  ```bash
  sudo docker compose exec -T core-api python -m scc_core.config attach-eval pipeline-v4-shorter-prompt --run - --by owner < the-run-file.json
  ```
  ```bash
  sudo docker compose exec core-api python -m scc_core.config activate pipeline-v4-shorter-prompt --by owner --reason "why"
  ```
  A version must keep every setting of the release's default with the same types; `create` says what differs. Activation refuses a pipeline version with no attached eval, or whose eval missed a threshold, until you add `--accept-warnings "why"`; the reason goes into the audit. Rollback never asks.
- **After an app rollback (RB-13):** the older release keeps the active version it finds. If `list` shows a version the newer release shipped (for example `pipeline-v5` after going back to a release that ships `pipeline-v4`), activate the older release's default, with `--accept-warnings` if asked.
- **Never:** edit the config tables by hand. Versions, evals and the audit are append-only (database triggers refuse changes); only the pointer moves.
- **Verify:** `list` shows the `*` on the version you meant; `history` shows your line first; for core, the next check's job has that `config_version`.

## RB-16 Someone asks about their data
**Status:** draft; the deletion and the purges are tested in `tests/integration/test_privacy.py` (P3.06, D-054), not yet exercised on the VM.
- **When:** an email to `contact@suppclaimcheck.com` asks for a copy of a person's data, a correction or a deletion. Answer within one month. What is kept, and for how long: [PRIVACY.md](PRIVACY.md).
- **Deletion:** admin's Users page, Delete on their row, type their email, confirm. The account, its checks and its sign-in records go at once; its LLM cost rows stay, unlinked. If the page says the deletion didn't finish, the hourly purge finishes it: check the row shows "being deleted" for no more than an hour. A person can also delete their own account under Account.
- **A copy:** their checks are on admin's Jobs page (search by email); open each trace for the text, claim and result. Send them as text. Their account row holds only the email, the role and dates.
- **Correction:** only the email address can be wrong. Create a new account with the right one, then delete the old one when they agree; checks don't move between accounts.
- **Checks that the purges run:** each module logs `purge finished` with counts (core at 03:30 UTC, web and the gateway hourly):
  ```bash
  sudo docker compose logs --since 25h core-worker web gateway | grep "purge"
  ```
  A core line `purge skipped: the active config has no [retention]` means the active pipeline version predates v8: see RB-15.
- **Verify:** the person's email no longer appears on Users; the reply is sent; the private log has the date and what was done (no personal details).


## RB-17 Link checks fail, or a site asks the reader to stay away
**Status:** draft; the reader, its address checks and the switch are tested in `services/gateway/tests/test_pages.py` and `tests/integration/test_article_links.py` (P3.12, D-055), and real pages were read through the dev stack; not yet exercised on the VM.
- **Symptoms:** a `url_blocked` alert (someone gave an address inside a private network, or one that redirects there); many link checks failing with `page_unavailable` or `page_timeout`; a `pages_not_configured` alert; or an email from a site owner about the page reader.
- **Checks:** which codes, how often, for which sites. The gateway logs the host, never the path:
  ```bash
  sudo docker compose logs --since 24h gateway | grep "page not read"
  ```
  Admin's Jobs page, filtered to failed: each trace shows the link and the code.
  - **`url_blocked` from one account, again and again:** someone may be probing. Nothing was fetched: the address is checked before any connection. Look at the account's other checks, and disable it on Users if it goes on.
  - **`page_unavailable` or `page_timeout` for every site:** the gateway can't reach the internet. Check its egress (RB-07 has the same check for the literature):
  ```bash
  sudo docker compose exec gateway python -c "import urllib.request; print(urllib.request.urlopen('https://www.nccih.nih.gov/robots.txt', timeout=10).status)"
  ```
  - **`pages_not_configured`:** the active gateway config has no `[pages]` (a rollback to gateway-v2 or older). Activate gateway-v3 or later (RB-15), then restart the gateway.
- **Switch link checks off, without a release:** export the active pipeline version, set `url = false` under `[inputs]`, name the copy (for example `pipeline-v9-no-links`), then create and activate it as in RB-15 with `--accept-warnings "links off: why"`. The new-check page stops offering links at once; checks already running finish. Activate the previous version to switch them back on.
- **A site owner asks the reader to stay away:** reply that `User-agent: SuppClaimCheck` with `Disallow: /` in their robots.txt does it within an hour (the `/reader` page says so). To stop at once, add their domain to `blocked_hosts` under `[pages]` in a gateway CLI version (RB-15: `gateway python -m scc_gateway.config`), activate it and restart the gateway; it covers every name under the domain. Note it in the private log.
- **Verify:** a link check to a page that should read does; for a blocked site, a link check ends with the "asks automated readers not to read this page" message.

---

## Release go-live checklist
Completed in P3.13 (2026-11-06). For each release, copy it into the private log and tick it there. The owner runs everything on the VM.

**Before the tag**
- [ ] The release PR is merged: it sets `VERSION` and turns `[Unreleased]` in `CHANGELOG.md` into the version's heading ([deploy/README.md](../deploy/README.md), Releases). CI is green on the merge commit.
- [ ] Evals: a replay of the recording passes in CI; any prompt, config or retrieval change since the last release has a live run in [EVALS.md](EVALS.md) that meets the thresholds.
- [ ] Security: no open Dependabot or secret-scanning alert, and every open CodeQL alert is fixed or dismissed with its reason ([security/OWASP-TOP10.md](security/OWASP-TOP10.md)). No entry in `.trivyignore.yaml` expires within the week.
- [ ] Runbook updated for anything new; [ERRORS.md](ERRORS.md) lists every new error code (a test checks it).

**The release**
- [ ] Tag the merge commit `vX.Y.Z` and push the tag. The Release workflow is green: all five images built, scanned and pushed with their attestations.

**On the VM, before the deploy**
- [ ] Copy every file under `deploy/` that changed since the running version. `git diff --stat v<running> v<new> -- deploy/` lists them. From 0.1.2 that is `compose.prod.yaml`, `backup.sh` and `restore.sh` (the same `scp` as first-time setup, then `sudo mv` into `/opt/suppclaimcheck/`).
- [ ] New settings in `/opt/suppclaimcheck/.env`: from 0.1.2, `SCC_OPERATOR_NAME=...`, so the privacy page names the controller (P3.06).
- [ ] Cloudflare: the `admin.suppclaimcheck.com` route and its Access application are in place, allowing only the owner (C1.2).
- [ ] Backup taken today (`journalctl -u scc-backup.service --since today`), and a restore verified within the last 30 days (RB-12).
- [ ] Feature flags as intended for this rollout stage (table below).
- [ ] The owner's go-ahead, recorded in the private log.

**The deploy**
- [ ] `sudo ./deploy.sh X.Y.Z` prints `smoke test passed` (RB-13). If it fails, `sudo ./rollback.sh`.

**After the deploy**
- [ ] `https://suppclaimcheck.com/version` names the new version; `https://suppclaimcheck.com/health/deps` answers `"status":"ok"`.
- [ ] `sudo docker compose ps`: every service healthy.
- [ ] UptimeRobot watches `https://suppclaimcheck.com/health/deps` for the keyword `"status":"ok"` (once; from 0.1.2 it watched `/health`, which stays up with the database down: RB-04).
- [ ] Admin opens through Cloudflare Access; Overview loads.
- [ ] Rotate anything the release notes ask for (the first release after 0.1.2: the NCBI API key and the Telegram bot token; [OWASP-TOP10.md](security/OWASP-TOP10.md), R-01).

## Feature flags per input type
Each input type has a switch in the pipeline config (versioned, P2.09), so it can be turned off without a release (RB-15, RB-17).

| Input | Switch | State in pipeline-v9 | Stage 1 (owner) | Stage 2 (testers) |
|---|---|---|---|---|
| Pasted text or ad text | none: always on | on | on | on |
| A link to a page (P3.12) | `[inputs] url` | on | on | on, unless the owner turns it off (RB-17) |
| Ad screenshot (P4.01) | added with the feature | not built | | |
| Pasted YouTube transcript (P4.02) | added with the feature | not built | | |

## Weekly ops checklist
Completed in P3.13 (2026-11-06). About 15 minutes, every Monday.
- [ ] UptimeRobot: any downtime last week? Log it if so
- [ ] `sudo docker compose ps`: every service `(healthy)`; anything unhealthy goes to RB-03
- [ ] healthchecks.io: all backups and disk checks green
- [ ] Disk usage below 70% (`df -h /`)
- [ ] Costs this month vs budget; any unusual user
- [ ] Admin: Reviews has nothing waiting; Injections shows no account held; failed jobs looked at
- [ ] Admin: Outcomes read (new feedback and what people said was missing); no account on Users "being deleted" for more than an hour
- [ ] Dependabot, CodeQL and secret-scanning alerts triaged; `.trivyignore.yaml` and the dated `exclude-newer-package` entries in `pyproject.toml` checked for dates that have passed
- [ ] Ubuntu updates applied; reboot the VM if required (`/var/run/reboot-required`)
- [ ] Monthly, the first Monday: a restore drill (RB-12)
