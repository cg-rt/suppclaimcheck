# Security review: OWASP Top 10

P3.07, reviewed 2026-11-06 on the M7 branch (release candidate for v1.0). Checked against the [OWASP Top 10:2025](https://top10.owasp.org/2025/), plus the [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/) for the model-specific risks. A full ASVS Level 1 checklist comes after v1 (D-035).

**How to read it.** Each item is **Pass** (the control exists and something proves it) or **Partial** (a control exists with a known gap, listed under [Residual risks](#residual-risks)). The evidence is a test, a scan, a header seen on the live site, or a drill. Nothing here is "Fail": the gaps are accepted or fixed by the owner in C3.4.

## Summary

| # | Category | Verdict |
|---|---|---|
| A01 | Broken Access Control | Pass |
| A02 | Security Misconfiguration | Pass |
| A03 | Software Supply Chain Failures | Pass |
| A04 | Cryptographic Failures | Partial (R-06) |
| A05 | Injection | Pass |
| A06 | Insecure Design | Pass |
| A07 | Authentication Failures | Partial (R-04, R-05) |
| A08 | Software or Data Integrity Failures | Pass |
| A09 | Security Logging and Alerting Failures | Partial (R-07, R-13) |
| A10 | Mishandling of Exceptional Conditions | Pass |

**Scans on 2026-11-06:**
- **Images:** all five built fresh pass the release gate (`trivy image --exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed`, Trivy 0.75.0). Three things had to change first (D-056): Debian updates at build, cloudflared 2026.10.7, and one scoped secret exception for the Anthropic SDK's docstrings.
- **GitHub:**
  - Dependabot: 0 open alerts.
  - Secret scanning (push protection on): 0 open.
  - gitleaks: green on every PR.
  - CodeQL: 2 open `py/url-redirection` alerts on `main`, both reviewed as false positives and hardened anyway (A01).

## A01 Broken Access Control: Pass

- **Each check belongs to its owner:** every job page, its live progress stream and its feedback form load the job through one function that answers 404 for anyone else's (`_own_job`). Tests: `test_a_check_is_only_visible_to_its_owner`, `test_feedback_is_saved_on_your_own_result_only`.
- **Admin, three gates:**
  1. Admin answers only on its own hostname; elsewhere it doesn't exist.
  2. Behind the tunnel, a request must carry Cloudflare Access's token.
  3. The session's role must be `admin`.

  The live admin hostname redirects to the Cloudflare Access login (checked 2026-11-06). Tests: `test_admin_answers_only_on_its_host_and_only_to_admins`, `test_behind_the_tunnel_admin_needs_the_cloudflare_access_header`, `test_without_an_admin_host_admin_does_not_exist`. The token is checked for presence, not verified (R-03).
- **Cross-site requests:** every form carries a CSRF token (synchronizer for signed-in forms, double-submit for sign-in), and cookies are `SameSite`. Tests: `test_sign_in_without_the_csrf_token_is_rejected`, `test_signed_in_forms_need_the_session_csrf_token`, `test_retry_needs_the_form_token_then_queues_the_failed_phase`.
- **Open redirects:** the sign-in `next` path is limited to same-site paths. Since P3.07 it also refuses spaces and control characters, because browsers drop tabs and line breaks from a URL. Tests: `test_next_path_stays_on_this_site`, `test_next_path_refuses_characters_browsers_drop`.
  - CodeQL's two `py/url-redirection` alerts are false positives: the sign-in path goes through that check, and the confirm redirect was a path on this site to a job already loaded. Since P3.07 that redirect uses the ID core returned.
- **Database least privilege:** one schema and one role per module, and no module can read another's tables. Tests: `test_other_modules_cannot_read_auth_users`, `test_other_modules_cannot_read_core_jobs`, `test_app_roles_cannot_change_tables`.
- **Server-side request forgery** (folded into A01 in 2025), for the page reader:
  - Only public addresses are reached, checked at connection time, redirects and robots.txt included.
  - DNS rebinding is closed by connecting to the address that was checked.
  - Tests: `test_bad_addresses_are_refused_before_any_connection`, `test_names_that_resolve_to_private_addresses_are_refused`, `test_a_redirect_into_the_private_network_is_refused` (D-055).
- **Network:** only `edge` and `gateway` reach outside. web, core and postgres sit on an internal network with no internet, and production publishes no ports. Tests: `test_internal_network_has_no_internet`, `test_only_edge_and_gateway_reach_outside`, `test_prod_publishes_no_ports_and_has_no_extra_networks`. The VM's firewall allows SSH only (C0.5).

## A02 Security Misconfiguration: Pass

- **Headers on every response, from web:**
  - A strict content security policy: scripts, styles and fonts from this site only, no inline code, `frame-ancestors 'none'`, `base-uri 'none'`, `object-src 'none'`.
  - Also: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, a `Permissions-Policy` that turns off camera, microphone, location and payment, and `Cross-Origin-Opener-Policy: same-origin`.
  - `Cache-Control: no-store` on every page.
  - No server banner.

  Seen on the dev stack and in production on 2026-11-06. Test: `test_sign_in_page_sets_headers_and_a_pre_session_csrf_cookie`.
- **HTTPS:** Cloudflare redirects HTTP to HTTPS and sends `Strict-Transport-Security: max-age=15552000; includeSubDomains` (checked in production 2026-11-06; C1.4).
- **No stack traces:** errors are a coded page with a reference. Tests: `test_an_unexpected_error_is_a_coded_500_without_a_trace`, `test_every_code_is_in_the_catalogue` (`docs/ERRORS.md`).
- **Containers:**
  - The three Python images run as a non-root user, without pip, and install Debian security updates at build (D-056).
  - Every base image is pinned by digest.
  - Secrets are Docker secret files, never environment values. Test: `test_secrets_are_files_never_environment_values`.
- **Fonts and scripts:** all self-hosted. Test: `test_static_files_are_cacheable_and_fonts_are_self_hosted`.
- **Admin:** it doesn't exist without an admin hostname. Test: `test_without_an_admin_host_admin_does_not_exist`.

## A03 Software Supply Chain Failures: Pass

- **Locked dependencies:** one `uv.lock`, installed with `uv sync --locked` in every image. Nothing newer than 14 days is taken, except dated security exceptions, each with a delete date (D-041; virtualenv's ended 2026-11-06).
- **Pinned builds:** base images are pinned by digest. GitHub Actions are pinned to full commit SHAs, and Trivy and gitleaks are checksum-verified binaries (D-023). The embedding model is pinned by revision and each file's SHA-256 (`tools/fetch_embedding_model.py`).
- **Scans:**
  - Dependabot (weekly, 14-day cooldown) and CodeQL run on GitHub.
  - Trivy gates every release on fixable CRITICAL and HIGH findings. Accepted findings live in `.trivyignore.yaml`, each with a reason and an expiry (D-040).
- **Provenance:** every release image carries a build provenance attestation (`gh attestation verify`, `deploy/README.md`).
- **Exceptions taken early, by the owner:** cloudflared 2026.10.7 and fastembed 0.9.1 (for Pillow 13) were both taken before they were two weeks old (D-056, D-041; R-10).

## A04 Cryptographic Failures: Partial

- **Passwords:** Argon2id (argon2-cffi defaults, RFC 9106). Old hashes are rehashed at sign-in. An unknown email is checked against a dummy hash, so it takes as long as a wrong password. Test: `test_hash_is_argon2id_and_verifies_only_the_right_password`.
- **Tokens:** session IDs and CSRF tokens come from `secrets.token_urlsafe(32)` (256 bits). Comparisons run in constant time.
- **In transit:** HTTPS at Cloudflare, and the tunnel is outbound TLS. Inside the VM, containers talk plain HTTP and plain PostgreSQL on isolated Docker networks (R-06).
- **At rest:** backups are encrypted by restic before they leave for R2 (EU jurisdiction). Secret files are `0444` inside a root-only `0700` folder. The database volume itself isn't encrypted (R-06).
- **Repository:** gitleaks in pre-commit and CI, and GitHub push protection. A Claude Code hook blocks reading or naming secret files.
- **Logs:** credentials in URLs are masked before any line is written, after the NCBI key and the Telegram token were found in gateway logs. Test: `test_secrets_in_urls_are_masked_wherever_they_appear`. Rotating both after the next deploy is R-01.

## A05 Injection: Pass

- **SQL:** every query takes its values as parameters (psycopg). The few f-strings in SQL insert identifiers from code (table and role names), never input.
- **HTML:** Jinja escapes everything (`select_autoescape`), and no template marks anything `|safe`. The content security policy forbids inline scripts. htmx runs with `allowEval: false` and `allowScriptTags: false`.
- **Commands:** the services run no shell commands. The only `subprocess` call reads the git commit in the eval CLI, with fixed arguments.
- **Prompt injection:** see the LLM section below.

## A06 Insecure Design: Pass

The threats were designed against from the start (the brief, D-011, D-021):
- **The model has no authority:** no tools, no actions. It reads, labels and copies sentences; code verifies every quote and computes the verdict and the score.
- **Spending is the user's choice:** nothing is searched or spent before a claim is confirmed. A monthly cap fails closed, with a reserve. Each account gets a daily quota (D-053).
- **Only the gateway reaches outside** (D-011), so one module holds every outside key and every outside call.
- **Requests are triaged before any search:** personal medical advice, marketing copy and abuse are refused before a search starts (POLICY.md, D-050).
- **Every change to prompts, config or retrieval is evaluated** (EVALS.md).

## A07 Authentication Failures: Partial

- **No sign-up:** accounts are created by an admin, with a generated password shown once. Test: `test_admin_cli_creates_accounts_and_refuses_duplicates`.
- **Guessing is limited:**
  - Five failures lock an account for the window, even for the right password.
  - Each network address is rate-limited across accounts.
  - Cloudflare blocks bursts on `/sign-in` (C1.4).

  Tests: `test_five_failures_lock_the_account_even_for_the_right_password`, `test_one_address_is_rate_limited_across_accounts`. A wrong email and a wrong password get the same message.
- **Sessions:**
  - Server-side, in a `__Host-` cookie (HttpOnly, Secure, SameSite=Lax), with a new ID at every sign-in.
  - They end after idle and absolute timeouts.
  - A password change ends the other sessions.

  Tests: `test_sign_in_issues_a_new_session_id_and_drops_a_planted_one`, `test_sessions_end_after_the_idle_and_the_absolute_timeout`, `test_password_change_ends_other_sessions`, `test_session_cookie_has_the_host_prefix_rules`.
- **Passwords:** 12 to 128 characters. They aren't checked against lists of breached passwords (R-05).
- **Second factor:** admin needs Cloudflare Access's one-time code by email as well. Users have no second factor (R-04).

## A08 Software or Data Integrity Failures: Pass

- **Releases:** images are built only from a `vX.Y.Z` tag that matches `VERSION`, scanned, and pushed with a provenance attestation. `deploy.sh` records a version only after its smoke test passes (D-040).
- **Config:** versions are append-only, enforced by database triggers even for the module's own role, with an audit trail of who, when, what and why. A changed repository file stops the deploy. Tests: `test_history_is_append_only_even_for_the_modules_own_role`, `test_a_changed_repo_file_stops_the_deploy`.
- **Untrusted data is never executed:** no pickle, no YAML object loading, no `eval`. The model's output is JSON, checked against a schema and retried once (P1.06).
- **No double charges:** every LLM call carries an idempotency key, so a retried job step is never charged twice. Test: `test_the_same_key_returns_the_stored_answer_without_a_charge`.

## A09 Security Logging and Alerting Failures: Partial

- **Logs:** JSON lines with a correlation ID on every request, job and internal call. They never hold the submitted text, an email or a credential.
- **Alerts go to Telegram,** carrying codes and IDs only:
  - failed jobs
  - budget warnings and the cap
  - an account throttled for hidden instructions
  - a page address pointing into a private network (`url_blocked`)
  - safety reviews and account deletions
  - failed backups
  - a disk over 80%

  healthchecks.io watches the backup and disk timers, and UptimeRobot watches `/health`.
- **Events kept as data, shown in admin:** injection events, dropped quotes (`hallucination_events`), sign-in attempts (30 days), and every LLM call with its cost.
- **Gaps (R-07, R-13):**
  - Container logs rotate at 5 × 10 MB per service, and D-015's Postgres log sink was never built, so old log lines are gone.
  - Repeated failed sign-ins are blocked but don't alert.
  - An unhealthy container is neither restarted nor reported (R-13).

## A10 Mishandling of Exceptional Conditions: Pass

- **Fail closed:** if the budget can't be checked, nothing starts (`budget_unavailable`). An unknown error is a coded 500 with a reference, never a trace.
- **Timeouts and retries:** every outside call has a timeout. Retries, with backoff and jitter, happen only on 429, 5xx and timeouts. Tests: `test_budget_retry.py`, `test_a_slow_site_times_out`.
- **"200 OK" with an error inside:** PubMed sometimes answers that way. It is read as an outage, not as "no articles" (fixed in M5).
- **Partial failures carry a note:** an article that won't download, or that the model didn't answer for, is left out with a note. A check that loses its worker is ended after 20 minutes with an alert, and can be retried from admin. Tests: `test_an_article_the_model_did_not_answer_for_leaves_a_note`, `test_the_sweep_ends_checks_stuck_in_a_working_state`.
- **Every error code** has a user message and a runbook entry (`docs/ERRORS.md`, completeness test).
- **Crash, don't hang:** the database-down rehearsal (RB-04, 2026-11-06) found core-worker hanging after the outage: alive, unhealthy and silent, so Docker never restarted it. It now reads its config before opening its connection pool, and exits when the database isn't there, so Docker's restart takes over. Test: `test_the_worker_exits_when_the_database_is_down_at_start`. What still isn't covered is R-13.

## LLM Top 10 (2025)

| Risk | Verdict | How it is handled |
|---|---|---|
| LLM01 Prompt injection | Partial (R-08) | Submitted text, pages and articles are wrapped as untrusted data with an instruction to ignore instructions in them. The model has no tools. Instructions it notices are flagged, recorded and throttle the account (articles never count against a user). Tests: `test_instructions_in_submitted_text_are_recorded_and_throttle_the_account`, `test_instructions_in_an_article_are_recorded_against_the_article`. A successful injection can at most mislabel a passage, and its quote must still exist word for word in the article. |
| LLM02 Sensitive information disclosure | Pass | Prompts hold no secrets or other users' data. Each call sees one claim and public articles. |
| LLM03 Supply chain | Pass | One provider over its API (D-033). The local embedding model is pinned by revision and file hashes. |
| LLM04 Data and model poisoning | Pass | No training or fine-tuning. Articles come only from PubMed and Europe PMC, with an allowed licence. |
| LLM05 Improper output handling | Pass | Output is schema-validated, quotes are verified against the source, everything shown is escaped, and nothing the model writes is executed. |
| LLM06 Excessive agency | Pass | No tools, no actions. The verdict and score are computed by code. |
| LLM07 System prompt leakage | Pass | The prompts are public in this repository and hold no secrets. Requests for them are refused (POLICY.md). |
| LLM08 Vector and embedding weaknesses | Pass | Embeddings are of public article passages only, made locally, and never mixed across users. |
| LLM09 Misinformation | Partial (R-09) | Verified quotes, a code-computed verdict, "Not found" as a valid answer, and a not-medical-advice notice on every result. The labels are still the model's judgement: verdict accuracy on the tuning set was 75 to 81% (EVALS.md). |
| LLM10 Unbounded consumption | Pass | Monthly caps per lane, a daily quota per account, `max_tokens` per stage, and a page read from the top only (30,000 characters). Tests: `test_budget_guard.py`. |

## Residual risks

For the owner to accept or reject in C3.4. Likelihood and impact are for this deployment: invite-only, a handful of users.

| ID | Risk | Likelihood · impact | What limits it now | Recommendation |
|---|---|---|---|---|
| R-01 | The NCBI API key and the Telegram bot token were written to production's gateway logs from 0.1.1 until the masking fix ships | Low · Low | Logs stay on the VM, which only the owner can reach; the masking ships with the next release | Rotate both right after the next deploy (already planned) |
| R-02 | Single machine at home: power, internet or hardware loss takes the site down | Medium · Medium | Auto-start after power loss and reboots, nightly encrypted off-site backups, a restore verified on the VM (C1.5), UptimeRobot alerts | Accept for v1 |
| R-03 | Admin checks that a Cloudflare Access token is present but doesn't verify its signature | Low · High | Web publishes no ports; the only way in is the tunnel, and Cloudflare Access stands in front of the admin hostname; an admin session and role are still required | Accept for the beta; verify the token (Cloudflare's public keys, audience) after v1 |
| R-04 | Users have no second factor | Low · Low | Invite-only, generated passwords, lockout and rate limits; a user can only see their own checks | Accept |
| R-05 | Passwords aren't checked against breached-password lists | Low · Low | 12-character minimum, generated initial passwords | Accept |
| R-06 | Inside the VM, traffic between containers and the database at rest aren't encrypted | Low · Medium | Isolated Docker networks; the VM is reachable only by SSH with a key; backups are encrypted | Accept |
| R-07 | Logs are short-lived (5 × 10 MB per container) and failed sign-ins don't alert | Medium · Low | Lockout and Cloudflare's rule block guessing; job traces, sign-in attempts and LLM calls are kept in the database | Accept for v1; the log sink (D-015) is in the backlog |
| R-08 | A prompt injection in an article could make the model mislabel a passage | Low · Medium | No tools; the quote must exist word for word; one article counts once; injection events are recorded and shown in admin | Accept |
| R-09 | The model mislabels passages, so a verdict can be wrong | Medium · Medium | Evals measure it (75 to 81% verdict accuracy), quotes are shown so a reader can judge, not-medical-advice notice, feedback under every result | Accept; keep measuring in the beta (P4.06) |
| R-10 | Runtime packages taken before the two-week rule (cloudflared 2026.10.7, fastembed 0.9.1) | Low · Medium | Pinned, scanned, chosen for security fixes, recorded in D-041 and D-056 | Accept |
| R-11 | The beta-carotene serious-illness notice is a known false positive (D-050) | High · Low | A notice too many, never a missing one; recorded in POLICY.md | Accept (already accepted on 2026-11-03) |
| R-12 | Model calls are processed by Anthropic, possibly in the United States | Certain · Low | Named on the privacy page with the no-training and 30-day terms; users are asked to leave out health details | Accept |
| R-13 | Docker restarts a container that exits, never one that is only unhealthy, and nothing alerts on an unhealthy container | Low · Medium | The worker hang found in the rehearsal is fixed; UptimeRobot on `/health/deps` sees the database, core-api and the gateway; the weekly checklist looks at `docker compose ps` | Accept for the beta; add an unhealthy-container check to the hourly host timer next |
