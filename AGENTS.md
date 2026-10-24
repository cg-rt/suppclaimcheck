# AGENTS.md: operating manual for SuppClaimCheck

Read this first, every session. It is the single source of agent instructions; `CLAUDE.md` imports it.

## 1. The project

- SuppClaimCheck checks claims about dietary supplements against open-access biomedical literature. A user submits a claim (text, ad text, article URL, ad screenshot or pasted YouTube transcript), confirms the extracted claim, and gets a verdict (Supported, Contradicted, Mixed, Insufficient evidence, Not found), a 0–100 score computed by code from a versioned rubric, and verbatim, verified quotes with full citations.
- It runs on the owner's home server (a Windows 11 mini PC) inside a Hyper-V Ubuntu VM, reachable only through Cloudflare Tunnel at `suppclaimcheck.com`. Invite-only. Budget: EUR 25 a month in total, enforced in code.
- It is a learning and portfolio project. The evidence it produces (eval results per version, cost per check, latency, incident records) matters as much as the features.

## 2. Owner and private context

- **If `private/AGENTS.private.md` exists, read it right after this file.** It describes the owner, how work is split and how to communicate.
- `private/` is a separate, private git repository holding the plan, status, contribution log, private decisions, lessons and personal context. This public repo ignores it (D-031). Agents without a `private/` folder work from this file alone.

## 3. Who runs what

- Agents write code, tests and docs, run routine local dev commands, commit on feature branches and open pull requests.
- The owner reviews and merges every pull request into `main`.
- The owner runs every production command on the VM: deploy, rollback, backup, restore, drills. Never run commands on the production VM yourself; prepare them for the owner.

## 4. Where things live

| Path | What |
|---|---|
| `private/` | Private repo, ignored here: `AGENTS.private.md`, `STATUS.md`, `PLAN.md`, `LOG.md`, `DECISIONS-private.md`, `OUTCOMES.md`, `lessons/`, `evidence/`, and `pii-patterns.txt` (the word list for the private-words check) |
| `docs/DECISIONS.md` | Technical decision records (D-nnn). Project-management decisions are in `private/DECISIONS-private.md` and share the numbering, so gaps are expected |
| `docs/RUNBOOK.md` | Incident procedures (RB-nn), release and weekly ops checklists. Linked from admin |
| `docs/EVALS.md` | Eval set description and results table |
| `docs/architecture/architecture.yaml` | Architecture map: single source for the viewer and the README diagram |
| `services/<module>/` | One folder per module: `edge`, `web`, `core`, `gateway`, `postgres` (from Phase 1) |
| `deploy/` | Compose files, `versions.env`, deploy, rollback, backup and restore scripts; `deploy/host/` builds the production VM |
| `evals/` | `cases/` (tuning) and `held_out/` (never used for tuning) |
| `tools/` | Developer tools, such as `check-private-words.mjs` |
| `.claude/` | Claude Code hooks |

## 5. Session protocol

**Start**
1. Read `AGENTS.md`. If `private/` exists, read `private/AGENTS.private.md`, then `private/STATUS.md`, the current phase in `private/PLAN.md` and the last five entries in `private/LOG.md`.
2. Check what is running in dev: `docker compose -f deploy/compose.dev.yaml ps`. If production state matters, ask the owner for `docker compose ps` output from the VM.
3. Tell the owner the current state and the next task in three lines. If the next task is the agreed next one, start it. Confirm first at a phase start, before changing the plan, and for anything the owner has to do.

**During**
- Work only on the current task. If the plan needs to change, propose it, get approval, then update `private/PLAN.md` and the right decisions file.
- One pull request per milestone (a group of related tasks named in the plan), not per task. Inside it, use small commits on a feature branch (`feat/p1-skeleton`) with conventional commit messages. Small doc fixes ride along with the next PR.
- Every PR body opens with **What to look at (10 min)**: three to five pointers into the diff, one command to try, and one thing that could go wrong.
- Write a DECISIONS entry only for a choice a reviewer might question. Smaller choices get one line in the PR body.

**End**
1. Rewrite `private/STATUS.md`.
2. Tick `private/PLAN.md` tasks, with one line of evidence each (PR, test name, date).
3. Append one entry for the session to `private/LOG.md`: five to eight bullets with contributor tags. Record failures and their causes too.
4. Add lesson notes to `private/lessons/`, and new terms to `private/lessons/GLOSSARY.md`.
5. Update `docs/architecture/architecture.yaml` once per milestone PR, if a module, port, dependency or version changed.
6. Commit both repos: the public one on a feature branch, and the private one with `git -C private add -A` and `git -C private commit -F <message file>`.

## 6. Never

- Never put personal details in this public repo: the owner's name, employers, clients, career, contact details, learning notes or plans. Never copy anything from `private/` into it. The command guard runs `tools/check-private-words.mjs` before every `git commit` and blocks words from `private/pii-patterns.txt`.
- Never commit secrets, `.env` files or keys. Never print a secret value in chat.
- Never tick a PLAN box without evidence: a test name, commit SHA, date, or a file in `private/evidence/`.
- Never disable, skip or weaken a test to make it pass.
- Never deploy, change production, or touch the VM without the owner's explicit go-ahead.
- Never push to `main` directly, force-push, skip hooks (`--no-verify`), or run `docker compose down -v` (it deletes data volumes).
- Never let an LLM produce the verdict or the score. Code computes both from the versioned rubric.
- Never give an LLM call tools or authority to act. Every submitted or fetched text is untrusted data.
- Never show a quote that failed verification. Never fill gaps from model knowledge; "Not found" is a valid answer.
- Never call an external service from any module other than `gateway`.
- Never download PDFs, scrape or automate YouTube, or ignore robots.txt and site terms. Process only articles whose licence is on the allowlist (D-017).
- Never spend money outside the budget lanes (D-021). Never raise a cap without the owner.
- Never add a dependency without a one-line justification in the PR, plus a DECISIONS entry if it is significant.
- Never write "proven", "cures", or medical or dosing advice in product text.
- Never reverse a recorded decision silently.

## 7. Engineering conventions

- Python 3.14, FastAPI, Pydantic v2, Jinja2 + htmx with server-sent events, Procrastinate, Alembic, uv (commit the lockfile), ruff, pytest, Playwright. Pin exact versions when a module is created; prefer releases at least two weeks old.
- Every module has its own folder, a Dockerfile (slim base pinned by digest, non-root user), a short `README.md`, `/health` and `/version`, and a committed OpenAPI spec. `edge` and `postgres` use their native health checks.
- The app has one semantic version for all modules, one root `CHANGELOG.md` and one tag `vX.Y.Z` (D-034). Each release builds every image.
- Each module owns one database schema and a least-privilege database role. Migrations are versioned per module.
- Logs are JSON to stdout plus the Postgres log sink. Every log line and internal HTTP call carries the correlation ID (`X-Correlation-ID`).
- External calls always have timeouts. Retry with exponential backoff and jitter only on transient errors (429, 5xx, timeouts).
- Idempotency keys on job submission and on internal side-effecting calls.
- Tests: unit tests for deterministic logic, a CI check that each committed OpenAPI spec matches the code plus a few API tests per module (D-035), recorded fixtures for external APIs (no live calls in CI), and an end-to-end smoke test after every deploy.
- Git: feature branches, PRs, CI green before merge, conventional commits, app tags `vX.Y.Z`, LF line endings. Commit as the owner's GitHub no-reply address (repo-local git config, in both repos).
- Pass commit messages and PR bodies as files (`git commit -F`, `gh pr create --body-file`). The command guard (`.claude/hooks/`) blocks any command that names a secret file, even in quoted text.
- CI: pin every GitHub Action to a full commit SHA (D-023).
- Dev on Windows: bind ports to 127.0.0.1 only and avoid 5432 and 5678, which another stack on this PC uses.

## 8. AI rules: the product's core promise

- Answers come only from retrieved passages. Code checks quotes verbatim; failures are dropped and logged as hallucination events.
- Structured output is validated against a schema. Invalid output is retried once, then the stage fails cleanly.
- Prompts, N, K, model, temperature (where the provider supports it), retrieval and rubric weights, quotas and feature flags are versioned config, never constants in code.
- Every LLM call records model, tokens in and out, cost, latency, stage, job and user.
- Any change to a prompt, config, model or retrieval needs an eval run, with results recorded in `docs/EVALS.md`.

## 9. UI rules

- Use the impeccable skill for all UI design work. Design tokens live in `docs/DESIGN.md` (from Phase 1).
- Restrained and editorial; take the character from scientific journals, citations and evidence hierarchies. Generous whitespace, sparse animation.
- Avoid: Inter, Roboto, Arial and system fonts as the design font; purple gradients; glowing cards; emoji as icons; generic SaaS layouts.
- WCAG 2.2 AA. Check 390 px, 320 px and desktop. Admin is dense and functional but uses the same design language.

## 10. Environment

- Dev: Windows 11 Pro, repo at `D:\SuppClaimCheck`, PowerShell, Docker Desktop (WSL2 backend, memory capped).
- Prod: Hyper-V VM `scc-prod`, Ubuntu Server 26.04 LTS, Docker Engine. The app lives in `/opt/suppclaimcheck`: compose file, version pins, secrets and volumes, no source code. Images are pulled from GHCR.
- Windows PowerShell 5.1 strips quotes inside arguments passed to programs, which breaks `gh --jq '"text \(.x)"'`. Read JSON with `ConvertFrom-Json`, or run such commands from Git Bash.
- Accounts and keys are listed by name only in `private/STATUS.md`. Values live in the owner's password manager and in Docker secrets on the VM.
