# Changelog

One version for the whole app (D-034). Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Checks use pipeline v9 (D-055): v8 plus article links below; a check from pasted text sends exactly what it sent under v8.
- Checks use pipeline v8 (D-054): v7 plus the retention periods below; prompts, search and scoring are unchanged.
- Checks use pipeline v6 (D-052): the search also tries other spellings of the effect and population words ("anaemia" finds "anemia" too), and studies whose title says the substance was injected or infused are listed as found but not read, unless the claim is about injections. The result page names each reason an article wasn't read. In the evals, iron claims are right again and verdict accuracy reaches 81%.
- "No claim found" is now a request for a clearer text instead of a failed check (pipeline v5).
- Checks use pipeline v1 (D-044, docs/METHOD.md): up to five open-access articles per claim instead of one, chosen by type of study (systematic reviews and trials first), each assessed separately; rubric v1 weighs each article by its type of study and needs human evidence for a verdict. The result page explains the score article by article. Verdicts can differ from 0.1.x for the same claim.
- The gateway keeps each article's MeSH headings, which tell human from animal and lab studies; article metadata cached before this release is fetched again once.
- Checks use pipeline v4 (D-047): hybrid retrieval with matching words weighted 0.7 and meaning 0.3, chosen by comparing three retrieval settings on the eval set. A check takes longer (about 20 seconds in testing) when its articles have not been read before; repeat articles are as fast as before.

### Added
- Design pass (P3.11):
  - **How a check works:** a public page at `/method`, linked from every page's footer, the new-check page and the score workings. It explains, in plain words, how articles are found and weighted, how quotes are checked and how the verdict and score are worked out, with the weights table, the verdict rules and a worked example. Its numbers are the active pipeline config's, kept equal by a test.
  - **The articles read, numbered:** the result page lists each article with its type of study, its weight and what it found (supports, contradicts, points both ways, or no finding). Each quote names its article ("Article 2: Moabedi et al., 2023") and links to it, instead of repeating the full author list, and the score workings name the article on each line.
- Article links (P3.12, D-055):
  - **New check from a link:** the new-check page offers "A link to a page" next to Text. SuppClaimCheck reads the page once, lists up to eight claims it makes about supplements, and the user picks one to check; the confirm and result pages name the page that was read.
  - **The page reader** is in the gateway and checks every address first: only http and https on the usual ports, and never an address inside a private network, also after redirects. It obeys robots.txt and `noai`, reads at most 2 MB in 10 seconds, and only web pages. A public page at `/reader` says what it does and how a site can keep it away.
  - **When a page can't be read,** the check stops before any model call with a message that says why and suggests pasting the text instead.
  - **A switch in the pipeline config** (`[inputs] url`) turns links off without a release (RUNBOOK RB-17).
  - **Config and migrations:** `pipeline-v9`, `gateway-v3`; core migration 0009.
- Privacy (P3.06, D-054, docs/PRIVACY.md):
  - **Privacy notice:** a public page at `/privacy`, linked from every page's footer. It says what is kept, why, for how long and who else sees it, and names the operator from `SCC_OPERATOR_NAME`, set on the server.
  - **Delete my account:** on the Account page, after your password. It removes the account and every check at once; admins can do the same for an emailed request.
  - **Retention:** checks are deleted 180 days after submission, sign-in attempts after 30 days, the account link on LLM cost records after 180 days, and stored model answers after 7 days, by purges in each module.
  - **Config and migrations:** `web-v1`, `pipeline-v8`, `gateway-v2`; migrations web 0004, core 0008, gateway 0005.
- Feedback and time to result (P4.03, P4.04):
  - **Under every result:** three optional questions (does the verdict look right, was it easy to understand, was anything missing). The result also says how long the search took.
  - **Admin's new Outcomes page:** the answers, what people said was missing, and the median and 90th-percentile time from submitting and from confirming to the result. Each job's trace shows its feedback and times.
- Budget guard (P3.04, D-053): a Telegram warning once a month at 50% and 80% of a budget, and a banner in admin; new checks and confirmations stop with a plain message when less than one check's worth is left; each account may start 25 checks a UTC day; admin's Costs page shows spend per account. Gateway config `gateway-v1`; gateway migration 0004.
- Resilience (P3.05, D-053): every LLM call carries an idempotency key, so a repeated job step never charges twice; a check whose claim matches one finished in the last 7 days reuses its result and says so; an article the model doesn't answer for, or that won't download, is left out with a note instead of failing the check; checks that lose their worker are ended after 20 minutes with an alert, so admin can retry them; core and the gateway answer unexpected errors with a code and the correlation ID, which web's error page quotes as a reference. `docs/ERRORS.md` lists every error code. Checks use pipeline v7 (v6 plus the reuse and stall settings).
- Admin pages on the admin hostname, behind Cloudflare Access and the admin role (P3.08-P3.10, D-049): jobs by status with search; a per-job trace (timeline, input and claims, every LLM call with tokens, cost and latency, search and passages, verified and dropped quotes, result, prompts, correlation ID and the log command) with a retry for failed jobs; recent errors with runbook links; costs and usage from the LLM call records; eval results per pipeline version; module health and versions; config versions and diffs (read-only); and users (create, reset password, disable). core-api and the gateway gained read-only admin endpoints for them. Production needs the `admin.suppclaimcheck.com` hostname and its Access application in Cloudflare first.
- Guardrails (P3.01-P3.03, D-050, docs/POLICY.md), with pipeline v5: before any search, a check is refused when it asks for personal medical or dosing advice, marketing copy, something off-topic, the system's set-up or abusive content, and stops with a question when it names no supplement or only a general effect ("good for you"). Nothing is searched or spent for either. Checks about pregnancy, children, serious illness, very high doses or medicines show a safety notice above the result and are flagged for the owner's review, with a Telegram alert that carries only the job ID and topic. Instructions hidden in a submitted text or in an article are ignored and recorded; an account whose texts keep containing them can't start new checks for a day.
- Admin: a safety review queue and an injection events page (P3.03). Reviews lists the checks flagged for a safety topic, oldest first; each job's trace shows the notice the user saw and a form to record whether it fit, wasn't needed or needs follow-up, with an optional note never shown to the user. Injections shows, per account, the checks that count towards the throttle and until when it holds the account, then every event. The overview counts both. Migration `core` 0007 adds the review columns.
- Versioned config store (D-048): every module's settings (pipeline prompts and retrieval, gateway prices and caps, web sign-in limits) are kept as versions in the database with one active version and an audit trail of who changed what, when and why. `python -m scc_<module>.config` lists versions, shows diffs, creates a version from a TOML file, activates one and rolls back. A new check uses the version active when it starts; gateway and web apply a new version on restart. Activating a pipeline version without a passing eval needs a stated reason. RUNBOOK RB-15.
- Hybrid retrieval (D-019, D-046), pipeline v3, active from pipeline v4 (see Changed): passages are ranked by matching words and by meaning (a local embedding model in core, nothing leaves the machine), fused with weighted reciprocal rank fusion; embeddings are cached per passage. The core image now carries the model (about 400 MB larger).
- Query v1 (D-042, D-045): the confirm page offers PubMed's spelling when a supplement name looks misspelled ("ashwaganda" → "ashwagandha"), applied only if you choose it; searches include a supplement's other names (ashwagandha also finds *Withania somnifera*); and when PubMed matches nothing, the result says the search found no articles instead of implying there is no evidence. Pipeline v2.
- Eval set v1 (30 labelled claims, all approved by the owner) and the eval runner, `python -m scc_core.eval`, with a free replay of the recorded run in CI (D-043). Development tooling: nothing changes for users.

### Fixed
- When PubMed's search was down but answered "200 OK" with an error inside, a check said "Not found" (and reuse would have repeated it for a week). It now retries and, if PubMed stays down, fails with "The literature databases are not answering".
- The migrate steps of a deploy wait for each other, so two can't collide on the same database grants ("tuple concurrently updated").
- Stylesheet links carry a hash of the file, so after a deploy no browser or Cloudflare cache pairs the new pages with old styles.
- The result page says "took 1 second", not "1 seconds".
- Pages still described one article per check: the new-check page now says a check reads up to five, and the result page says "The 5 articles that were read".
- History scrolled sideways on phones when a link check was named by a long address; it wraps now. On the narrowest phones the name and New check share the header's first row again.
- Admin on phones: tables keep states, models and IDs whole and scroll inside their card instead of breaking words down the column; the Reset, Disable and Delete actions are larger targets with short button labels (the email stays in the name screen readers hear); the overview's status tiles show as links.
- `backup.sh` waits up to 10 minutes for the backup storage host to resolve before starting. A nightly backup missed while the host PC was off runs right at boot, and on 2026-10-26 it failed because DNS through the Hyper-V Default Switch was not answering yet. Host script only: copy it to the VM; no new images.
- `restore.sh` prints the scratch-database drop command with `sudo`, which the VM needs.
- core-worker hung after a database outage: alive, unhealthy and silent, so Docker never restarted it and no check ran until someone did (found by rehearsing RB-04, P3.13). It now reads its config before opening its connection pool and exits when the database isn't there, so Docker restarts it until the database is back.

### Security
- Security review (P3.07): `docs/security/OWASP-TOP10.md` checks the OWASP Top 10:2025 and the LLM Top 10 item by item, with evidence, and lists 13 residual risks for the owner.
- The release scan passes again (D-056). With the 2026-11-06 vulnerability database, every image failed it:
  - **The Python images** now install Debian security updates at build, which fixes `apt` CVE-2026-109518.
  - **edge** uses cloudflared 2026.10.7, which fixes CVE-2026-63209 and CVE-2026-116587; it was taken before it was two weeks old.
  - **One scoped exception:** `.trivyignore.yaml` excuses Trivy's "Google key file" match only inside the Anthropic SDK's own folder. Those matches are docstring examples, not keys.
- The sign-in page's way back refuses spaces and control characters, and the confirm and feedback redirects use the job ID core returned (the two CodeQL `py/url-redirection` alerts; neither was exploitable).
- Logs no longer contain the NCBI API key or the Telegram bot token. The HTTP client logs every request's full URL, and the key is a query parameter, the token part of the path, so both were written to the gateway's container logs (in production since 0.1.1). Every log line is now masked before it is written. Rotating both after this release is recommended: a new key in the NCBI account and a new token from @BotFather, then restart the gateway.
- Pillow 13.0.0 in the core image (41 advisories in 12.3.0, including GHSA-5x8w-8jj5-q52h and GHSA-v9pv-5gc8-w2m6), through fastembed 0.9.1, the first release that allows Pillow 13, taken before it is two weeks old (D-041). Core embeds text only and never opens an image; 0.1.2 has no Pillow at all, so this reaches production with the first release that has hybrid retrieval.

## [0.1.2] - 2026-10-25

The first upgrade of the live site, with fixes found during the first production deploy.

### Security
- certifi 2026.10.19 (CA bundle, GHSA-6q48-6c7w-9gw2) in the web, core and gateway images, taken before it is two weeks old (D-041); its scan exception is removed.

### Fixed
- `backup.sh` alerts (Telegram and healthchecks.io) when `secrets/restic.env` is missing or not valid shell. Before, bash stopped on the syntax error before the alert was set up, so the backup failed silently. The alert text is fixed, because bash's own message quotes the bad line, which may hold a secret.
- `/health` answers `HEAD` with 200 instead of 405, for uptime monitors that probe with HEAD.
- core-worker's job start and end log lines carry the job's correlation ID in the `correlation_id` field, not only inside the message.

### Changed
- `deploy/README.md`: the `restic.env` example has no bracket placeholders to paste by mistake, and notes the EU endpoint for EU-jurisdiction buckets; the Telegram row says to press Start in the bot's chat first.

## [0.1.1] - 2026-10-25

The first release. `v0.1.0` was tagged but never released: its workflow logged out of GHCR before the attestation step, so only an unsigned edge image was pushed (`suppclaimcheck-edge:0.1.0`); do not deploy it.

### Added
- Walking-skeleton modules: `edge` (Cloudflare Tunnel), `web`, `core` (`core-api` and a placeholder `core-worker`), `gateway`, `postgres` (PostgreSQL 18 + pgvector). Each has a Dockerfile with a base image pinned by digest and a non-root user.
- `/health`, `/health/deps` and `/version` on every HTTP module; JSON logs with a correlation ID passed between modules (`X-Correlation-ID`).
- Development and production compose files: an internal network with no internet, egress only for `edge` and `gateway`, Docker secrets, no host ports in production.
- One database schema and least-privilege role per module, with per-module Alembic migrations run as a one-off step; `auth.users` table.
- Jobs API in core-api (`POST /jobs` with an `Idempotency-Key`, `GET /jobs/{id}`, `POST /jobs/{id}/confirm`) and a Procrastinate worker in core-worker; jobs carry their correlation ID.
- Pipeline v0: claim extraction, confirmation, PubMed query by code, one open-access article from Europe PMC, JATS passages ranked by Postgres full-text search, passage assessment, verbatim quote verification with hallucination events, and rubric v0 for the verdict and score.
- Gateway: schema-constrained Claude calls with a monthly cap per lane that fails closed, cost from a versioned price table, retries on transient errors only, and a record of every call; PubMed and Europe PMC with the licence allowlist, a rate limiter and an article cache.
- Committed OpenAPI specs for web, core and gateway, checked against the code in CI.
- Login in web: invite-only accounts from an admin CLI (`python -m scc_web.admin`), Argon2id hashes, server-side sessions in a `__Host-` cookie with idle and absolute timeouts, CSRF tokens on every form, sign-in lockout and rate limit, password change (D-039).
- Web pages: sign in, new check, confirm the claim, live progress over server-sent events, result (verdict, score with its arithmetic, verified quotes with citation and licence, what was searched, what could not be checked, not-medical-advice notice), history and account. Design tokens in `docs/DESIGN.md`.
- core-api: `GET /jobs?user_id=` (history) and `GET /jobs/{id}/events` (progress).
- Browser smoke test with axe accessibility checks (`tests/e2e`), run against a running stack.
- Release workflow: a `vX.Y.Z` tag builds all five images, scans them with Trivy (fixable critical or high findings fail it, accepted ones expire), pushes them to GHCR and attaches build provenance attestations (D-040).
- Deploy tooling: `deploy.sh` (pull, pre-deploy backup, migrate, start, smoke test; `versions.env` updated only after the smoke test passes), `rollback.sh`, and a smoke test (`python -m scc_web.smoke`) run as a one-off container (D-040, RB-13).
- Backups: nightly `pg_dump` through restic to Cloudflare R2 with retention (7 daily, 4 weekly, 6 monthly), `restore.sh` for drills into a scratch database or a full replace, an hourly disk check, systemd timers and Docker log rotation (RB-11, RB-12, RB-06).
- Alerts: gateway `POST /notify` sends Telegram messages, with quiet codes and repeats held back for an hour; the gateway alerts when the budget cap refuses a call, core when a job fails; host scripts alert directly (RB-09).
- `tools/rehearse_deploy.py` rehearses deploy, rollback, backup and restore on Docker Desktop with production's compose file.

### Changed
- Python images no longer ship pip, and the postgres image installs Debian security updates at build time (P1.11).
- edge uses cloudflared 2026.10.3, taken before it was two weeks old for a certificate-validation fix (D-040).

### Fixed
- Release workflow: stays logged in to GHCR until the build provenance attestation is stored.
