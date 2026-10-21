# Claude Code hooks

`AGENTS.md` tells agents what never to do; these hooks make Claude Code actually stop. They are wired up in `.claude/settings.json` and apply to every Claude Code session in this project.

| Hook | When it runs | What it does |
|---|---|---|
| `guard-secret-files.mjs` | Before Read, Edit, Write, NotebookEdit and Grep | Blocks secret files: `.env*` (except `.env.example`), `secrets/`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `restic-password*`, `*credentials*.json`, `.ssh/` |
| `guard-commands.mjs` | Before every Bash or PowerShell command | Blocks force-push, pushing to `main`, `--no-verify` / `commit -n` / `SKIP=`, deleting Docker volumes, `git clean -x`, `ssh`/`scp`/`sftp`, and commands that name a secret file. Before any `git commit`, it runs `tools/check-private-words.mjs` and blocks the commit if it would publish private details or anything under `private/` (D-031) |
| `stop-reminder.mjs` | When the agent tries to finish | If code changed after `private/STATUS.md` and `private/LOG.md` were last saved, asks once for the end-of-session steps, then lets the agent stop. Stays quiet when there is no `private/` folder |

## How to spot it
A blocked action shows up in the transcript with a reason starting "Blocked by project hook: …". The guards **fail closed**: if a guard script crashes or Node is missing, the action is blocked (`onFailure: "block"`). The Stop reminder fails open, so it can never trap the agent.

## The private-words check
The word list lives in `private/pii-patterns.txt`, which is never committed, so publishing the check doesn't publish the words. Findings name a word by its number, not its text. Run it by hand on everything that would be committed:
```bash
node tools/check-private-words.mjs --all
```
On a machine without `private/`, the check skips itself.

## How to test
```bash
node --test .claude/hooks/hooks.test.mjs
```
Each test sends a hook the same JSON Claude Code would send and checks the decision. Quoted text and heredoc bodies, such as commit messages, are not treated as commands.

## Limits
- They bind Claude Code only. Pre-commit (planned) and GitHub push protection cover every committer.
- An agent could edit these files. Changes to `.claude/` show up in the pull request, so review them there.
- To pause them on one machine, set `"disableAllHooks": true` in `.claude/settings.local.json` (not committed). Remove it again afterwards.
