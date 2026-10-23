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
- **Decision:** one public monorepo. Feature branches; PRs with CI green before merge; conventional commits; per-module tags `<module>/vX.Y.Z`; LF line endings.

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
