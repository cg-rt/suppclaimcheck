# Data inventory and retention (P3.06, D-054)

What SuppClaimCheck stores, where, why, for how long, and how it is deleted. The public privacy notice (`/privacy`) is the plain-language version of this page. `tests/integration/test_privacy.py` checks two things: every table in the `auth`, `core` and `gateway` schemas is named here, and the numbers on `/privacy` match the configs each module ships as active.

## The rules

- **Keep only what a check needs.** A check holds the text a person submitted, and nothing else about them. Their email address lives only in web's `auth` schema.
- **Every period is versioned config**, not a constant: `web-v1` `[retention]`, `pipeline-v8` `[retention]`, `gateway-v2` `[retention]` and `[idempotency]`. A module whose active config has no `[retention]` purges nothing and logs that it skipped.
- **Each module purges its own schema** (least privilege, D-037):
  - **web:** every hour, inside its process.
  - **core:** a nightly Procrastinate task at 03:30 UTC.
  - **gateway:** every hour, inside its process.
  - **One copy at a time:** web and the gateway hold a Postgres advisory lock while their purge runs (`scc_common.periodic`).
- **Deleting an account is immediate** and runs in this order:
  1. web disables the account and ends its sessions;
  2. core deletes its checks, then asks the gateway to unlink its LLM calls;
  3. web deletes the account row and its sign-in attempts.

  Each step can run again. If a step fails, the account stays closed and marked (`deletion_requested_at`), and web's hourly purge finishes the deletion.
- **Text in alerts and logs:** Telegram alerts and logs never carry submitted text or email addresses, only codes and numbers (D-024, P1.14).

## Tables

**Retention** says how long a row is kept. **Deleted by** says what removes it: **A** is account deletion, **P** is the purge.

### web: `auth`

| Table | Personal data | Purpose | Retention | Deleted by |
|---|---|---|---|---|
| `auth.users` | Email address, password hash (argon2id), role, created, disabled, deletion requested and by whom | Accounts (invite-only) | Until the account is deleted | A |
| `auth.sessions` | Account number, hash of the session ID, CSRF token, times | Signed-in sessions | Until the session ends (60 min idle, 12 h absolute in `web-v0`/`v1`) | A (cascade), P (ended sessions) |
| `auth.login_attempts` | Email as typed, network address, success | Lockout and rate limit; looking into attacks | `login_attempts_days` (30) | A (that email), P |
| `auth.config_versions`, `auth.config_active`, `auth.config_audit`, `auth.config_evals` | A role name as actor (`deploy`, `agent`, `owner`) | Versioned config (D-048) | Kept | – |
| `auth.alembic_version` | None | Migration state | Kept | – |

### core

| Table | Personal data | Purpose | Retention | Deleted by |
|---|---|---|---|---|
| `core.jobs` | Account number; submitted text; extracted and confirmed claim; result; safety topics; review by an admin (admin's account number and note); times (P4.04) | The check itself | `check_days` (180) after submission | A, P |
| `core.job_events` | None beyond the check (states, codes; "retry by admin user N") | Progress and trace | With its check | A, P (cascade) |
| `core.passages` | None (article text) | What was read | With its check | A, P (cascade) |
| `core.hallucination_events` | None (quotes that failed verification) | Quality evidence | With its check | A, P (cascade) |
| `core.injection_events` | Account number | Throttle and abuse review (P3.02) | With its check | A, P (cascade) |
| `core.feedback` | Account number; answers; free text "anything missing" | Feedback on results (P4.03) | With its check | A, P (cascade) |
| `core.embeddings` | None (vectors of article passages, keyed by text hash) | Retrieval cache | Kept | – |
| `core.procrastinate_jobs`, `core.procrastinate_events` | None (task arguments are check IDs) | Task queue | Finished tasks: `queue_days` (30) | P |
| `core.procrastinate_periodic_defers`, `core.procrastinate_workers` | None | Queue schedule and worker heartbeats | Kept by Procrastinate | – |
| `core.config_versions`, `core.config_active`, `core.config_audit`, `core.config_evals` | A role name as actor | Versioned config | Kept | – |
| `core.alembic_version` | None | Migration state | Kept | – |

### gateway

| Table | Personal data | Purpose | Retention | Deleted by |
|---|---|---|---|---|
| `gateway.llm_calls` | Account number; check ID; the stored answer (`output`), which can repeat the claim | Budget, costs, idempotent retries (D-053) | Row kept (cost record). `output` cleared after `output_retention_days` (7). `user_id` cleared after `user_link_days` (180) | A (unlink and clear), P |
| `gateway.erased_users` | Number of a deleted account, nothing else | Lets the purge unlink a call that was still running when the account was deleted | Kept | – |
| `gateway.articles` | None (article metadata and full text) | Literature cache | Kept | – |
| `gateway.budget_alerts` | None | One warning per level and month | Kept | – |
| `gateway.config_versions`, `gateway.config_active`, `gateway.config_audit`, `gateway.config_evals` | A role name as actor | Versioned config | Kept | – |
| `gateway.alembic_version` | None | Migration state | Kept | – |

## Outside the database

| Where | What | Retention |
|---|---|---|
| Container logs (Docker `json-file`) | JSON lines with account and check numbers, codes, and a network address only on a blocked sign-in. Never submitted text, email addresses or secrets (`scc_common.logs.redact`) | 5 × 10 MB per container, rotated; recreated containers (every deploy) start empty |
| Backups (restic, Cloudflare R2, EU jurisdiction, encrypted) | The whole database | Nightly copies: 7 daily, 4 weekly, 6 monthly (D-024). Pre-deploy copies: the last 5 |
| Telegram | Alerts to the owner: check and account numbers and codes | Until the owner deletes them in the chat |
| Anthropic (Claude API) | Submitted text, claims and article passages, for extraction and assessment | Not used for training; deleted within 30 days unless flagged for a usage-policy breach or required by law (Anthropic's commercial terms and privacy centre, checked 2026-11-05). May be processed in the US |
| PubMed (NCBI) and Europe PMC | Search terms built from the claim (supplement, effect, population words). No account, no submitted text | Their own logs |
| Cloudflare Tunnel | All traffic, terminated at Cloudflare | Cloudflare's own logs |

## After a restore

A restored backup brings back accounts deleted after it was taken. Every deletion sends a Telegram alert, `account_deleted`, naming the account number. After a real restore (RB-12), delete again every account those alerts name since the backup's time, from admin's Users page or with the same erasure calls.

## Requests by email

These go to `contact@suppclaimcheck.com`, and the owner answers within a month:

- **A copy of the data:** the account's checks from admin's Jobs page, plus the account row.
- **Deletion:** admin's Users page, Delete, typing the email to confirm.
- **Correction:** of the email address.

## Who is named as the controller

The privacy page names the operator from `SCC_OPERATOR_NAME`, an environment setting on the server. Without it, the page says "run by the person who invited you". The name is never in this repository.
