# DECISIONS

Technical decision records. Newest at the bottom. Never edit a past decision; supersede it with a new record that names the one it replaces.
**Decided by:** [Owner] the project owner decided · [Together] the agent proposed and the owner approved · [Agent] agent default that the owner can reverse.
Prices and versions were checked online on 2026-10-21.
Project-management decisions are kept in a private repository and share this numbering, so gaps are expected (D-006, D-009, D-010, D-029). On 2026-10-21 this file was redacted to remove personal details (D-031). No technical decision changed.

---

### D-000 · 2026-10-21 · Founding constraints · [Owner]
- **Decision:** host on a mini PC at home, exposed only through Cloudflare Tunnel, with no open router ports; containers stay portable for a later cloud move. Invite-only access; the owner creates accounts. Email and password authentication first, designed for Google sign-in later. EUR 25 a month in total, enforced in code. LLM provider to be chosen by comparison, behind one swappable interface. Literature: open-access biomedical (PubMed, open-access full text). The user confirms the claim before any spending. Config changes need no approval step but are versioned, audited and reversible. Sensitive topics are flagged, not held. `AGENTS.md` is the single source of agent instructions and `CLAUDE.md` imports it.

### D-001 · 2026-10-21 · Name and domain · [Owner]
- **Decision:** SuppClaimCheck on its own domain, `suppclaimcheck.com` (Cloudflare Registrar, about US$10–11 a year). Admin on `admin.suppclaimcheck.com`.
- **Alternatives:** a subdomain of the owner's personal site; the names Footnoted or Assay.
- **Trade-offs:** costs about EUR 0.80 a month from the budget; a clean separation from the personal site.

### D-002 · 2026-10-21 · Verdicts and score meaning · [Owner], presentation rule [Together]
- **Decision:** five verdicts: Supported, Contradicted, Mixed, Insufficient evidence, Not found. One 0–100 score: 0 = consistent strong evidence against, 50 = balanced or too weak to call, 100 = consistent strong human evidence for. The verdict label separates the cases a 50 alone cannot. The score is never shown alone: always with the verdict, the breakdown and the article count.
- **Alternatives:** two scores (direction and strength); only the original three verdicts.
- **Trade-offs:** one number is easy to read but invites false precision; the presentation rule limits that.

### D-003 · 2026-10-21 · Scope · [Owner]
- **Decision:** supplements only; drug mentions only trigger the interaction safety notice and review flag. English only at first.
- **Alternatives:** adding OTC or all drug claims; Romanian input; a bilingual UI.
- **Trade-offs:** smaller scope keeps the policy and evals manageable. Romanian can be added later as an input language.

### D-004 · 2026-10-21 · Repository licence · [Owner]
- **Decision:** MIT.
- **Alternatives:** Apache-2.0 (patent grant, more paperwork); AGPL-3.0 (discourages reuse).

### D-005 · 2026-10-21 · Contact address · [Owner]
- **Decision:** `contact@suppclaimcheck.com` via Cloudflare Email Routing (free, forwards to the owner's mailbox). Used for NCBI, providers, Cloudflare alerts and the privacy notice.
- **Alternatives:** a new dedicated mailbox; the owner's personal address (it would end up in logs and the public privacy notice).

### D-007 · 2026-10-21 · Where it runs: Hyper-V VM for production, Docker Desktop for development · [Owner]
- **Context:** Windows Update reboots the PC. Docker Desktop is a development tool and does not support starting before someone signs in.
- **Decision:** production runs in a Hyper-V VM (`scc-prod`, Ubuntu Server 26.04 LTS, Docker Engine) set to start automatically with Windows, with no sign-in needed. Development stays on Docker Desktop. The source repo stays on `D:\SuppClaimCheck`, not in the WSL file system, because that is where the agents work. Production keeps only compose, config, secrets and volumes on the VM's own disk and pulls images by version.
- **Alternatives:** Docker Engine inside WSL2 (start-at-boot relies on an undocumented community workaround); Docker Desktop with automatic sign-in (weaker physical security, and Docker calls it dev-only).
- **Trade-offs:** about 10 GB of RAM reserved for the VM, a second OS to patch, and two Docker engines on one PC (dev and prod, clearly separated). Starting without sign-in must be proven by a reboot drill.

### D-008 · 2026-10-21 · YouTube input: pasted transcript only · [Owner]
- **Context:** YouTube's Terms forbid automated access and downloading, so transcript-scraping libraries and `yt-dlp` are out. The Data API only downloads captions when the video owner allows it.
- **Decision:** the user pastes the transcript from YouTube's own "Show transcript" panel, plus the link for citation. The service never calls YouTube.
- **Alternatives:** the Gemini API reading a public video link (Google's own route; a preview feature costing a few cents an hour); deferring the input type.
- **Trade-offs:** clunkier for users, and timestamps depend on what they copy. No cost and no terms risk.

### D-011 · 2026-10-21 · Six containers instead of fourteen · [Together]
- **Decision:** `edge` (cloudflared), `web` (public site, admin and login), `core-api` and `core-worker` (one `core` module, one image), `gateway` (LLMs, literature sources, URL fetching, alerts), `postgres` (with pgvector, also hosting the queue).
- **Merges and why:** login lives in `web` as its own package with its own schema and database role, so core, which handles hostile text, still cannot read password hashes. Admin is in `web` but served only on its own hostname behind Cloudflare Access. The AI gateway and the sources connector are one `gateway`: still the only module that calls outside services (`edge` only carries the tunnel) and the only holder of API keys, so the budget cap can't be bypassed. Embeddings run inside core-worker. Backups and host checks are systemd timers on the VM, not a container.
- **Deviation from the founding constraints:** authentication is a separate module (package, schema, role) but not a separate container. It can be re-split later.
- **Trade-offs:** public and admin code ship in one image. URL fetching shares a process with the LLM keys; mitigated by strict SSRF rules and size and time limits.

### D-012 · 2026-10-21 · Docker Compose on one host; no Kubernetes · [Together]
- **Decision:** Compose gives restart policies, health checks, networks and secrets. Kubernetes would add a control plane, ingress and manifests (weeks of learning unrelated to the product) for one host and one operator. If it ever spans several machines, k3s is the next step.

### D-013 · 2026-10-21 · Core stack · [Together]
- **Decision:** Python 3.14 for all our modules; FastAPI with Pydantic v2 (OpenAPI generated from code, structured output validated); server-rendered Jinja2 + htmx with server-sent events for live progress; PostgreSQL 18 + pgvector 0.8; Alembic; uv; ruff; pytest; Playwright. Exact versions are pinned when each module is created.
- **Alternatives:** Python 3.15 (released 9 Oct 2026, libraries not caught up); TypeScript with Next.js (a second toolchain and build chain); Django (more than needed).
- **Trade-offs:** one language the owner can read; little JavaScript, so it is accessible by default.

### D-014 · 2026-10-21 · Job queue in Postgres (Procrastinate) · [Together]
- **Decision:** Procrastinate: retries with backoff, locks, deduplication, periodic tasks, and no Redis. Job states live in our own `core` tables, so the queue library can be swapped.
- **Alternatives:** Celery or RQ with Redis (another stateful container); a hand-rolled `SKIP LOCKED` queue (more code to own).
- **Trade-offs:** the project has a small maintainer team and is asking for more maintainers.

### D-015 · 2026-10-21 · Logs and traces · [Together]
- **Decision:** JSON logs to stdout with a correlation ID, plus a best-effort Postgres log sink with 30-day retention for the admin log view. The per-job trace (stages, LLM calls, costs, timings) lives in Postgres tables. Loki, Alloy, Jaeger and OpenTelemetry are deferred to the backlog.
- **Alternatives:** Grafana Alloy → Loki plus OpenTelemetry → Jaeger (three more containers); a full Grafana or SigNoz stack. Promtail is end of life since March 2026.
- **Trade-offs:** when the database is down, logs are only in `docker compose logs`; the runbook covers this.

### D-016 · 2026-10-21 · Authentication design · [Together]
- **Decision:** server-side sessions in a `__Host-` cookie (HttpOnly, Secure, SameSite=Lax), rotated at login, with idle and absolute timeouts; Argon2id via argon2-cffi; CSRF tokens; rate limiting and lockout; roles `user` and `admin`. The admin second factor comes from Cloudflare Access (an email one-time code); in-app TOTP is deferred.
- **Alternatives:** JWTs (hard to revoke, plus key management); hosted auth (another processor of user data, and teaches less).

### D-017 · 2026-10-21 · Literature sources and licences · [Together]
- **Context:** NCBI retired the PMC OA Web Service and the FTP packages in August 2026; the PMC Cloud Service on AWS replaces them. Europe PMC's API returns each article's licence and its JATS XML full text without a key (tested 2026-10-21).
- **Decision:** search PubMed via E-utilities (API key, registered tool and email, throttled at 8 requests/s, cached by PMID). Licence and full text from Europe PMC, with the PMC Cloud Service as fallback. No PDFs: XML keeps sections and exact wording, while PDF extraction breaks the verbatim quote check. Licence allowlist: CC0, CC BY, CC BY-SA, CC BY-ND, CC BY-NC, CC BY-NC-SA, CC BY-NC-ND. All allow text mining for this non-commercial project; revisit if it ever becomes commercial.
- **Trade-offs:** open-access-only means many claims end as Insufficient evidence or Not found. The result page lists relevant articles that were found but could not be assessed.

### D-018 · 2026-10-21 · Evidence ranking · [Together]
- **Decision:** systematic reviews and meta-analyses, then RCTs, other human trials, observational studies, narrative reviews, animal studies and in-vitro studies; PubMed relevance breaks ties. Deterministic, and explained on the method page.

### D-019 · 2026-10-21 · Embeddings and retrieval · [Together]
- **Decision:** local embeddings with fastembed (`bge-small-en-v1.5`) inside core-worker; vectors in pgvector, cached by passage hash and model. Keyword relevance from Postgres full-text search, fused with weighted reciprocal rank fusion. The weight, passage size and K are config.
- **Alternatives:** a TEI model-server container; API embeddings (another provider, and claim text leaves the machine); Qdrant or Elasticsearch.
- **Trade-offs:** Postgres full-text ranking is not true BM25; acceptable at this scale. Only one embedding model is evaluated (a domain model is in the backlog).

### D-020 · 2026-10-21 · LLM providers: bake-off, provisional choice · [Together]
- **Superseded in part by D-033 (2026-10-23):** v1 runs on Claude only; the bake-off below moves to after v1. The price and data-terms notes stay as reference.
- **Decision:** compare three providers on the eval set in Phase 2. Prices are USD per million tokens, input/output:
  - Anthropic: Claude Haiku 5.5 at 0.10 / 0.50; Sonnet 5.5 at 2 / 10. API data not used for training. Processing is global or US-only, so EU-only would need AWS or Google Cloud.
  - Google: Gemini 3.1 Flash-Lite at 0.25 / 1.50; Gemini 3.6 Flash at 0.75 / 3.75, doubling on 1 Jan 2027. Paid-tier data not used to improve products. EU regions only via Vertex AI.
  - Mistral: Small 4 at 0.15 / 0.60; Medium 3.5 at 1.50 / 7.50. EU company with EU hosting. Training opt-out terms to be verified.
- **Provisional model** for Phases 1–2: Claude Haiku 5.5, the cheapest candidate with schema-checked structured output. Disclosure: the planning agent is a Claude model; the bake-off decides with numbers.
- **Also:** temperature is set only where the provider supports it, because some current models reject sampling parameters. OpenAI was left out (gpt-5.4 mini/nano sit in the same price band and add nothing these three lack); it can be added through the interface.
- **Estimate:** about US$0.004–0.01 per text check on the small models.

### D-021 · 2026-10-21 · Budget lanes · [Together]
- **Decision:** EUR 25 a month in total: domain about EUR 0.80, live checks EUR 15, evals EUR 5, buffer about EUR 4. Warnings at 50% and 80%; new jobs stop at the cap. The price table and EUR rate live in config. The agent may spend up to EUR 1 per session from the eval lane without asking. Spend limits in each provider console are a second safety net.

### D-022 · 2026-10-21 · Build and deploy · [Together]
- **Decision:** CI builds images and pushes them to GHCR by version, with build attestations. The owner deploys on the VM with `deploy.sh` (pull, migrate, restart, smoke test); rollback means deploying the previous version.
- **Alternatives:** a self-hosted CI runner on the PC (rejected: on a public repo, a stranger's pull request could run code on the PC); automatic deploys.

### D-023 · 2026-10-21 · CI supply chain · [Together]
- **Context:** in March 2026 attackers rewrote most `aquasecurity/trivy-action` version tags to steal CI secrets.
- **Decision:** pin every GitHub Action to a full commit SHA, with Dependabot updating the pins. Use gitleaks, CodeQL, Trivy (0.35.0 or later, pinned), dependency review and Dependabot. release-please is deferred.

### D-024 · 2026-10-21 · Backups · [Together]
- **Decision:** a nightly `pg_dump`, encrypted by restic to Cloudflare R2 (10 GB free), keeping 7 daily, 4 weekly and 6 monthly. Run by systemd timers on the VM with healthchecks.io pings. A restore drill at Phase 1 and again in Phase 4, then monthly.

### D-025 · 2026-10-21 · Notification channel · [Together]
- **Decision:** a Telegram bot: free, private, phone push, one HTTPS call.
- **Alternatives:** the public ntfy.sh server (anyone who guesses the topic can read it); email (slow to notice).

### D-026 · 2026-10-21 · Uptime monitoring · [Together]
- **Decision:** an external UptimeRobot check on `/health`, plus healthchecks.io heartbeats for the backup and disk timers. Both on free personal plans.

### D-027 · 2026-10-21 · Network and access · [Together]
- **Decision:** no open router ports. The only way in is Cloudflare Tunnel. The admin hostname is behind Cloudflare Access (free up to 50 users) and the app's admin role. The internal Docker network has no internet; only `edge` and `gateway` sit on the egress network. Hostnames stay one level deep so Cloudflare's free certificate covers them.

### D-028 · 2026-10-21 · Repository conventions · [Together]
- **Decision:** one public monorepo. Feature branches; PRs with CI green before merge; conventional commits; per-module tags `<module>/vX.Y.Z` (superseded by D-034: one app tag `vX.Y.Z`); LF line endings.

### D-030 · 2026-10-21 · Claude Code guard hooks in Node.js · [Together]
- **Decision:** the hooks are small Node.js scripts in `.claude/hooks/`, with no npm packages. The two guards fail closed (`onFailure: "block"`); the Stop reminder fails open. Scope approved by the owner: block reading as well as editing secret files, and block `ssh`/`scp`/`sftp` so production stays the owner's. Added by the agent in the same spirit: block pushing to `main`, `SKIP=` (which turns off pre-commit checks), and `git clean -x`.
- **Alternatives:** PowerShell scripts (Windows-only, harder to test); Python (not installed at the time); bash with jq (jq not installed).
- **Trade-offs:** a second runtime next to Python, but Node 24 was already installed and the scripts only use its standard library. Matching is pattern-based, so a command that only mentions a secret path in text is also blocked; the workaround is passing text through files (AGENTS.md section 7).

### D-031 · 2026-10-21 · Public code, private project notes · [Owner]
- **Context:** the first public push included the owner's personal details in the plan, the log and the agent instructions.
- **Decision:** this public repo holds the app and its engineering documents only: code, technical decisions, runbook, evals, architecture map and agent rules. The plan, status, contribution log, lessons, personal context and project-management decisions live in `private/`, a separate private repository that this repo ignores. The original GitHub repository is deleted and recreated with a clean history. Authorship appears as the GitHub handle `cg-rt` only, including the licence.
- **Safeguards:** `/private/` in `.gitignore`; a private-words check (`tools/check-private-words.mjs`), which the Claude Code command guard runs before every `git commit`, with the word list kept in `private/`; wiring the same check into pre-commit, and a CI check that no tracked path sits under `private/`, follow with the pre-commit and CI work.
- **Trade-offs:** the public repo no longer shows the detailed contribution log; the owner tells that story elsewhere. Two repositories to commit to.

### D-032 · 2026-10-23 · Pre-commit checks · [Agent]
- **Decision:** the pre-commit framework runs gitleaks, ruff (check and format), the trailing-whitespace and end-of-file fixers, and the private-words check (D-031) on every commit, whoever makes it. pre-commit itself is pinned in a repo-root uv project that holds developer tools only (`pyproject.toml` with a `dev` group, `uv.lock` committed, not a package); uv takes only releases at least 14 days old, indirect dependencies included. Hook repos are pinned to commit SHAs with the tag in a comment, as D-023 does for Actions.
- **Alternatives:** `uv tool install pre-commit` (per machine, version not in the repo); `gitleaks-docker` (Docker must be running for every commit); `gitleaks-system` (a separate binary to install and keep updated).
- **Trade-offs:** the first run after install takes about 2 minutes while pre-commit builds gitleaks (it downloads Go if needed). Node must be on the PATH for the private-words hook. The root project is not a uv workspace; whether modules join one is decided when the first module is created.

### D-033 · 2026-10-23 · Claude only for v1; provider bake-off after v1 · [Owner]
- **Context:** D-020 planned accounts with three LLM providers in Phase 0 and a bake-off in Phase 2 (P2.08). Phases 1–2 already ran on Claude Haiku 5.5 alone, so the other two accounts would sit unused, and the bake-off adds work to a schedule that is already late.
- **Decision:** v1 uses Anthropic only (Claude Haiku 5.5 as provisional model, Sonnet 5.5 available for comparison within Anthropic). Only the Anthropic account and spend limit are set up. Whether to compare other providers is decided after v1.
- **Kept:** the gateway's provider interface stays swappable (P1.06), so adding a provider later is one adapter plus an eval run. Model choice remains versioned config, and any model change still needs an eval run (AGENTS.md section 8).
- **Trade-offs:** no measured cross-provider comparison in v1, so the model choice is defended by Claude's own eval results and cost, not by a comparison, and the planning agent is itself a Claude model (D-020's disclosure). No fallback during an Anthropic outage: checks fail cleanly and are retried later. Processing stays global or US-only; EU-only processing would need another provider.

### D-034 · 2026-10-24 · One version for the whole app · [Owner]
- **Context:** D-028 planned a version, changelog and tag per module, and CI that builds only the modules a change touches. For five small modules, one operator and a four-week window, that is bookkeeping without a matching benefit.
- **Decision:** the app has one semantic version, one root `CHANGELOG.md` and one tag `vX.Y.Z`. Each release builds, scans and pushes every image with that version. `deploy/versions.env` holds one line; `deploy.sh <version>` and `rollback.sh` act on the whole stack. `/version` still reports the version and git SHA in every module. Amends D-028 (per-module tags) and D-022 (deploy by version).
- **Alternatives:** per-module versions (D-028 as written): independent releases and rollbacks, at the cost of path-filtered CI, five changelogs and a version matrix to reason about.
- **Trade-offs:** no rollback of one module alone; every release rebuilds all images (minutes, not hours). Re-splitting versions later is a backlog item.

### D-035 · 2026-10-24 · v1 scope trims · [Owner]
- **Context:** a review of the plan at the start of Phase 1 found work sized beyond what v1 needs.
- **Decision:**
  - Contract tests: CI checks that each committed OpenAPI spec matches the code, plus a few hand-written API tests per module. Schemathesis moves to after v1.
  - Auth: no identity-provider abstraction for a future Google sign-in; refactor if that is ever built. Every security control in D-016 stays.
  - Live evals run from the CLI on the developer machine, inside the eval budget lane (D-021). No LLM key is stored in GitHub; CI runs only the free tier with recorded responses.
  - Security review: OWASP Top 10 checklist, security headers and clean scans. The full ASVS level 1 checklist moves to after v1.
  - Admin: the runbook is linked (not rendered), the architecture map becomes a health and version table, config activation and rollback stay on the CLI with a read-only versions and diff page, and logs are shown per job trace plus a recent-errors list rather than a general log browser.
- **Trade-offs:** less breadth in admin and in the security write-up; the rules that matter most (budget cap, quote verification, guardrails, evals) are unchanged.

### D-037 · 2026-10-24 · Module code layout, database roles and dev ports · [Agent]
- **Decision:**
  - **One uv workspace** at the repo root (completes D-032's open question): the Python modules (`services/web`, `services/core`, `services/gateway`) and a shared library, `libs/common` (`scc_common`: JSON logs, correlation IDs, the ops endpoints, database helpers), share one `uv.lock`. Images build from the repo root; `.dockerignore` lets in only the files they need, so `private/` can never reach a build context.
  - **Two roles per module:** `scc_<module>_owner` (no login) owns the schema and its tables; `scc_<module>_app` is the module's login, with row access to its own schema only. The migrate step connects as the superuser, creates or repairs roles and grants (idempotent, so password rotation is: change the secret, rerun), then runs Alembic under `SET ROLE` to the owner role. Only the one-off migrate containers hold the superuser secret.
  - **Dev ports:** Docker can't publish a port from an `internal` network, so in dev only, the modules with a 127.0.0.1 port also join a `devhost` network. `core-worker` and `postgres` stay internal-only, so the egress test still holds in dev. Production has no host ports and no such network (`tools/tests/test_compose.py`).
- **Alternatives:** separate lockfiles per module (dependency drift, five copies of the shared code); migrations as the app role (the app could then alter its own tables); one role per module that both migrates and runs (same problem); a dev reverse proxy instead of `devhost` (another container for a dev-only gap).
- **Trade-offs:** a change to `uv.lock` rebuilds every image's dependency layer. In dev, web, core-api and gateway can reach the internet; only production enforces the full rule.

### D-038 · 2026-10-24 · Pipeline v0: LLM calls, budget reservation, fixtures · [Agent]
- **Decision:**
  - **Anthropic SDK, its retries off.** The gateway calls Claude through the official `anthropic` SDK with `max_retries=0`; the gateway's own policy retries 429, 5xx and timeouts only, with exponential backoff and full jitter, so the policy is tested once and applies to every provider. Structured output uses `output_config.format` with a JSON schema made from core's Pydantic models; core validates the reply against the same model and retries an unusable reply once.
  - **No temperature.** The Messages API rejects `temperature` on every model since 2026-10-21, so none is sent (D-020's "only where supported"). Thinking is off and effort is set per stage in `pipeline-v0.toml`.
  - **Worst-case reservation for the cap.** Before each call the gateway reserves the call's worst-case cost under an advisory lock and refuses if the month's spend plus the reservation passes the lane's cap; afterwards the row gets the real cost. Concurrent calls can't slip under the cap together, and a database failure blocks the call (fails closed). `SCC_BUDGET_CAP_OVERRIDE_EUR` can lower caps for drills and never raise them.
  - **Who owns what.** Core owns prompts, the query, ranking, quote checking and the rubric (versioned in `pipeline-v0.toml`). The gateway owns the price table, the caps, the licence allowlist and the outside calls (`gateway-v0.toml`). Untrusted text always travels as a separate `data` field that the gateway wraps in delimiters.
  - **Fixtures.** Literature fixtures are recorded from the real services by `tools/record_fixtures.py`, with fields the gateway never reads removed. The LLM fixtures are hand-written in the API's response format until a dev key exists (C1.1); their quotes are cut from the real passages.
- **Alternatives:** raw HTTP to the Messages API (the skill guidance and the SDK's typed errors favour the SDK); checking the cap after the call (one expensive call could overshoot it); a cap check without reservation (two calls at once could both pass).
- **Trade-offs:** the SDK adds a second HTTP library (`httpx2`) next to `httpx`. A reserved worst case makes the cap refuse slightly early near the limit. The PMC Cloud Service fallback (D-017) is not built in v0: if Europe PMC is down, jobs fail cleanly (RB-07).

### D-039 · 2026-10-24 · Web: visual world, sessions and live progress · [Owner] [Agent]
- **Decision:**
  - **Visual world: "Warm consumer app"** (owner's pick in the M3 design round, over the rolled "Package leaflet" and the "Evidence Facts panel"). One 720px column of soft white cards, one orange used only for things you can press, Nunito Sans for our copy and Source Serif 4 italic for quoted sentences. Tokens in `docs/DESIGN.md`; the direction contract in `.impeccable/surfaces/services-web.md`.
  - **Server-side sessions** in `auth.sessions`, keyed by the SHA-256 of a random ID held in a `__Host-` cookie; idle and absolute timeouts and lockout thresholds live in versioned config (`web-v0.toml`). A new ID at every sign-in; a password change ends every other session. CSRF: synchronizer token per session, double-submit cookie on the sign-in form only.
  - **Live progress:** web's server-sent-events stream polls core-api once a second and sends the stage list only when the state changes; the page works without JavaScript by refreshing itself.
  - **Accounts** come only from the admin CLI (`python -m scc_web.admin`), with the password read from a hidden prompt or stdin.
- **Alternatives:** signed cookie sessions (can't be revoked server-side); WebSockets or Postgres LISTEN/NOTIFY for progress (more moving parts for a one-minute job); the leaflet world (the owner preferred a familiar app surface for testers on phones).
- **Trade-offs:** the warm consumer look is closer to common app templates, which the design round flagged; the evidence-first content and the two type voices carry the identity. Polling costs two small internal calls per open progress page per second.

### D-040 · 2026-10-24 · Release, deploy, backup and alert mechanics · [Owner] [Agent]
- **Decision:**
  - **Release workflow without third-party build actions.** A `vX.Y.Z` tag must match `VERSION`; plain `docker build` and `docker push`, Trivy as a checksum-verified binary (like gitleaks, D-023), and GitHub's own `actions/attest-build-provenance` pinned by SHA. Fixable critical or high findings fail the release; accepted ones go in `.trivyignore.yaml` with a reason and an expiry date, so an exception can't outlive its reason. gosu in the postgres image is skipped: it only drops root privileges at start-up and makes no network or TLS calls.
  - **Smaller images:** the Python images drop pip (its vendored urllib3, msgpack and setuptools were the scanner's findings), and the postgres image installs Debian security updates at build time with PostgreSQL itself held.
  - **cloudflared 2026.10.3 before it is two weeks old** (owner's call): 2026.10.0 carried a certificate-validation flaw (CVE-2026-111943) in the process that holds the tunnel. The two-week rule still holds for everything else.
  - **deploy.sh** records a version in `versions.env` only after its smoke test passes, so the file always names the last good version; `rollback.sh` returns to it after a failed deploy, or to the previous version after a good one. Every deploy takes a pre-deploy backup first. Migrations stay forward-only; a removing change ships in two releases.
  - **Smoke test** runs as a one-off container on the internal network with its own secret: health, version, sign-in, history, and one canned claim (about EUR 0.01 from the live lane; skipped on rollback or with `--no-claim`). `deploy.sh` creates or updates the smoke account with `admin ensure-user`.
  - **Backups** use restic's `--stdin-from-command`, so a failed `pg_dump` leaves no snapshot rather than a truncated one. Restores go into a scratch database by default; replacing the live database makes a safety backup first, pins the snapshot ID before it, and asks for a typed confirmation.
  - **Alerts:** the gateway owns Telegram for app events (it is the only module with egress); it alerts on its own when the cap refuses a call, and core reports job failures by code only, never user text. Quiet codes and one-hour de-duplication are versioned config. Host scripts call Telegram directly through `notify.sh`, so an alert still arrives when the app is down.
- **Alternatives:** docker/build-push-action and the Trivy action (more third-party code with write access to packages; the Trivy action's tags were hijacked in March 2026); `pg_dump | restic --stdin` (a failed dump still makes a snapshot); automatic rollback on a failed smoke test (a rollback right after a migration can be the wrong call; the owner decides with RB-13); core calling Telegram (core has no egress by design).
- **Trade-offs:** a canned claim on every deploy costs a little budget and depends on the LLM provider being up; `--no-claim` exists for outages. Removing pip means debugging inside a container can't `pip install`. Builds of the postgres image are not byte-for-byte reproducible because of the security updates.

### D-041 · 2026-10-25 · Security fixes newer than two weeks in the uv lock · [Owner] [Agent]
- **Decision:** when a Dependabot security alert's fix is newer than the two-week rule, add a dated per-package entry under `[tool.uv.exclude-newer-package]` in `pyproject.toml`, set just after the patched release, and lock that version. Each entry names the advisory and the date to delete it, the day the global "14 days" catches up. First use: filelock 4.2.1 and virtualenv 21.14.10, dev-only tools under pre-commit, and certifi 2026.10.19, which is in the web, core and gateway images (owner's call: a critical advisory in the CA bundle). A runtime package needs the owner's approval, as cloudflared did in D-040.
- **Alternatives:** drop `exclude-newer` and rely on Dependabot's cooldown (it doesn't cover a local `uv lock`); exempt the package for good with a short duration (weaker for every later release); wait two weeks (Dependabot's security runs fail every night meanwhile, and the alerts stay open).
- **Trade-offs:** a fixed cutoff gets stricter than the global rule once it passes, so a forgotten entry blocks later updates for that package; the comment carries the delete date. certifi's fix reaches production only with the next release.

### D-043 · 2026-10-28 · Eval set format and the record-and-replay eval tier · [Agent]
- **Decision:**
  - **Cases are TOML files**, one per case, in `evals/cases/` (tuning) and `evals/held_out/`, read with Python's built-in `tomllib`, so core needs no YAML dependency. `evals/evalset.toml` holds the set version and the pass thresholds. Every label starts as `draft`; only the owner sets `approved` or `disputed` (C2.1), and every run reports how many labels are approved.
  - **The runner drives the real pipeline functions** (extract, auto-confirm the first claim, search, assess, verify, score) in a throwaway database that it creates and drops, with its LLM calls charged to the gateway's eval lane. Job alerts are answered inside the runner and never reach Telegram.
  - **Record and replay at the core-to-gateway boundary.** A live run records every gateway reply, keyed by path and a hash of the request body without job ID, user ID and lane. CI replays the recording on every pull request: free, offline, about ten seconds. A rubric change sends the same requests, so CI shows its effect; a prompt, model, query or retrieval change sends new requests, the cases go stale and CI fails until someone runs a live eval and commits the new recording. That turns AGENTS.md section 8's "any change needs an eval run" into a check.
  - **Regression gate:** `evals/baseline-tuning.json` lists each case's outcome. CI fails when a case that passes in the baseline fails now. An intended change updates the baseline in the same pull request (`--write-baseline`), so the review diff shows which cases flipped. Thresholds are reported but don't fail CI until the guardrails exist (Phase 3); today some categories can't pass by design.
  - **Held-out cases run live only**, need a written reason, and are never recorded, so they can't be replayed or tuned against.
  - **Results are files** in git (`evals/runs/`, plus a row in `docs/EVALS.md`), not database tables: they are reviewable in pull requests, and the admin page (P3.09) can read them.
- **Alternatives:** recording at the gateway's outside calls (`scc_common.recorded`, as the canned-claim test does), which would replay the literature but still need the Anthropic SDK path and key per stage; mocking the LLM with hand-written answers (tests the plumbing, not quality); storing results in Postgres now (not reviewable in a PR); running evals only on demand (relies on people remembering).
- **Trade-offs:** recordings hold article full texts (open-access licences on the allowlist, gzipped, stored once each) and grow the repository by a few MB per live run. A replay can't measure latency, and it repeats one model sample, so it says nothing about run-to-run variation; only live runs measure that.
