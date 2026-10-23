// Tests for the guard hooks. Run: node --test .claude/hooks/hooks.test.mjs
// Each test feeds a hook the same JSON Claude Code would send, and checks its decision.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync, execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, mkdirSync, rmSync, utimesSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HOOKS = fileURLToPath(new URL('.', import.meta.url));

// Without CLAUDE_PROJECT_DIR the guard takes the test's folder as the project, so results don't depend on the machine.
const ENV = { ...process.env };
delete ENV.CLAUDE_PROJECT_DIR;

function run(script, payload) {
  const result = spawnSync(process.execPath, [join(HOOKS, script)], { input: JSON.stringify(payload), encoding: 'utf8', env: ENV });
  const out = result.stdout.trim();
  return { code: result.status, output: out ? JSON.parse(out) : null };
}

const isDenied = (r) => r.output?.hookSpecificOutput?.permissionDecision === 'deny';

function fileTool(tool, path) {
  const toolInput = tool === 'Grep' ? { pattern: 'x', path } : { file_path: path };
  return run('guard-secret-files.mjs', { hook_event_name: 'PreToolUse', tool_name: tool, tool_input: toolInput });
}

// Command tests run in a folder that is not a git repo, so results never depend on
// which branch this checkout is on. The "pushing from main" rule has its own tests below.
const NO_REPO = mkdtempSync(join(tmpdir(), 'scc-hook-norepo-'));
after(() => rmSync(NO_REPO, { recursive: true, force: true }));

function shell(command, tool = 'Bash', cwd = NO_REPO) {
  return run('guard-commands.mjs', { hook_event_name: 'PreToolUse', tool_name: tool, tool_input: { command }, cwd });
}

// --- Secret-file guard -------------------------------------------------------

const secretFiles = [
  ['Read', '.env'],
  ['Read', 'D:\\SuppClaimCheck\\.env.local'],
  ['Write', 'deploy/secrets/db_password.txt'],
  ['Edit', 'certs/server.key'],
  ['Read', 'C:\\Users\\someone\\.ssh\\id_ed25519'],
  ['Read', 'restic-password.txt'],
  ['Grep', 'secrets'],
];
for (const [tool, path] of secretFiles) {
  test(`secret-file guard blocks ${tool} ${path}`, () => {
    const r = fileTool(tool, path);
    assert.equal(r.code, 0);
    assert.ok(isDenied(r), JSON.stringify(r.output));
  });
}

const normalFiles = [
  ['Read', '.env.example'],
  ['Read', 'docs/RUNBOOK.md'],
  ['Edit', 'services/core/app/keys.py'],
  ['Write', 'deploy/make-dev-secrets.sh'],
  ['Read', 'docs/security/ASVS-L1.md'],
];
for (const [tool, path] of normalFiles) {
  test(`secret-file guard allows ${tool} ${path}`, () => {
    const r = fileTool(tool, path);
    assert.equal(r.code, 0);
    assert.equal(r.output, null);
  });
}

// --- Command guard -----------------------------------------------------------

const blockedCommands = [
  'git push --force origin feat/x',
  'git push -f',
  'git push --force-with-lease',
  'git push origin +feat/x',
  'git push origin main',
  'git push origin HEAD:main',
  'git commit --no-verify -m wip',
  'git commit -n -m wip',
  'SKIP=gitleaks git commit -m wip',
  "$env:SKIP='gitleaks'; git commit -m wip",
  'docker compose down -v',
  'docker compose -f deploy/compose.prod.yaml down --volumes',
  'docker volume rm scc_pgdata',
  'docker system prune -a --volumes',
  'ssh scc-prod "docker compose ps"',
  'scp deploy/compose.yaml scc-prod:/opt/suppclaimcheck/',
  'git clean -fdx',
  'cat .env',
  'Get-Content secrets\\db_password.txt',
  'type C:\\Users\\someone\\.ssh\\id_ed25519',
];
for (const command of blockedCommands) {
  test(`command guard blocks: ${command}`, () => {
    const r = shell(command);
    assert.equal(r.code, 0);
    assert.ok(isDenied(r), JSON.stringify(r.output));
  });
}

const allowedCommands = [
  'git push -u origin feat/p0-1-claude-hooks',
  'git push --follow-tags origin feat/x',
  'git commit -m "docs: explain why we never use --no-verify or git push --force"',
  'git commit -F msg.txt',
  "git commit -F - <<'EOF'\nfix: guard against docker compose down -v\nEOF",
  'docker compose down',
  'docker compose -f deploy/compose.dev.yaml up -d',
  'ssh-keygen -t ed25519',
  'cat .env.example',
  'git clean -fd',
  'node --test .claude/hooks/hooks.test.mjs',
];
for (const command of allowedCommands) {
  test(`command guard allows: ${command.split('\n')[0]}`, () => {
    const r = shell(command);
    assert.equal(r.code, 0);
    assert.equal(r.output, null);
  });
}

test('command guard blocks any push while main is checked out', () => {
  const dir = tempRepo();
  try {
    const r = shell('git push -u origin feat/x', 'Bash', dir);
    assert.ok(isDenied(r), JSON.stringify(r.output));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard allows pushing from a feature branch', () => {
  const dir = tempRepo();
  try {
    execFileSync('git', ['switch', '-c', 'feat/x'], { cwd: dir, stdio: 'ignore' });
    const r = shell('git push -u origin feat/x', 'Bash', dir);
    assert.equal(r.output, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- Pushing with git -C -----------------------------------------------------
// The branch that counts is the one in the repo git -C points at. The private notes repo (D-031)
// is pushed from the project root with `git -C private push`, straight to its main.

const slash = (path) => path.replace(/\\/g, '/');
const featureBranch = (dir) => execFileSync('git', ['switch', '-c', 'feat/x'], { cwd: dir, stdio: 'ignore' });

// A project on main whose private/ folder is its own repo, also on main.
function projectWithPrivateRepo() {
  const dir = tempRepo();
  initRepo(join(dir, 'private'));
  return dir;
}

test('command guard blocks git -C into a repo on main, even from outside any repo', () => {
  const dir = tempRepo();
  try {
    const r = shell(`git -C "${slash(dir)}" push -u origin feat/x`);
    assert.ok(isDenied(r), JSON.stringify(r.output));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard allows git -C into a feature-branch repo while the session repo is on main', () => {
  const session = tempRepo();
  const other = tempRepo();
  try {
    featureBranch(other);
    assert.equal(shell(`git -C "${slash(other)}" push -u origin feat/x`, 'Bash', session).output, null);
  } finally {
    rmSync(session, { recursive: true, force: true });
    rmSync(other, { recursive: true, force: true });
  }
});

test('command guard allows pushing the private notes repo with git -C private push', () => {
  const dir = projectWithPrivateRepo();
  try {
    assert.equal(shell('git -C private push', 'Bash', dir).output, null);
    assert.equal(shell('git -C private add -A && git -C private commit -F msg.txt && git -C private push', 'Bash', dir).output, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard still blocks pushing the project repo from main when a private repo exists', () => {
  const dir = projectWithPrivateRepo();
  try {
    assert.ok(isDenied(shell('git push -u origin feat/x', 'Bash', dir)));
    assert.ok(isDenied(shell('git -C private push && git push -u origin feat/x', 'Bash', dir)));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard blocks git -C private push when private/ is not its own repo', () => {
  const dir = tempRepo();
  try {
    mkdirSync(join(dir, 'private'));
    const r = shell('git -C private push', 'Bash', dir);
    assert.ok(isDenied(r), JSON.stringify(r.output));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard blocks a push whose -C folder is not a git repo', () => {
  const dir = tempRepo();
  try {
    featureBranch(dir);
    const r = shell('git -C does-not-exist push', 'Bash', dir);
    assert.ok(isDenied(r), JSON.stringify(r.output));
    assert.match(r.output.hookSpecificOutput.permissionDecisionReason, /not a git repo/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard reads a quoted -C path that contains spaces', () => {
  const session = tempRepo();
  const other = tempRepo('scc hook test-');
  try {
    featureBranch(other);
    assert.equal(shell(`git -C "${slash(other)}" push`, 'Bash', session).output, null);
  } finally {
    rmSync(session, { recursive: true, force: true });
    rmSync(other, { recursive: true, force: true });
  }
});

test('command guard reads Git Bash /d/... paths after -C', { skip: process.platform !== 'win32' }, () => {
  const session = tempRepo();
  const other = tempRepo();
  try {
    featureBranch(other);
    const posix = slash(other).replace(/^([a-zA-Z]):/, (_, drive) => `/${drive.toLowerCase()}`);
    assert.equal(shell(`git -C ${posix} push`, 'Bash', session).output, null);
  } finally {
    rmSync(session, { recursive: true, force: true });
    rmSync(other, { recursive: true, force: true });
  }
});

// --- Stop reminder -----------------------------------------------------------

function initRepo(dir) {
  mkdirSync(dir, { recursive: true });
  const git = (...args) => execFileSync('git', ['-c', 'user.email=test@example.com', '-c', 'user.name=test', ...args], { cwd: dir, stdio: 'ignore' });
  git('init', '-b', 'main');
  writeFileSync(join(dir, 'README.md'), 'test\n');
  git('add', '.');
  git('commit', '-m', 'init');
  return dir;
}

function tempRepo(prefix = 'scc-hook-test-') {
  return initRepo(mkdtempSync(join(tmpdir(), prefix)));
}

const HOUR_AGO = new Date(Date.now() - 3600_000);

// Creates private/STATUS.md and private/LOG.md, optionally with an old modification time.
function privateDocs(dir, mtime) {
  mkdirSync(join(dir, 'private'), { recursive: true });
  for (const name of ['STATUS.md', 'LOG.md']) {
    const path = join(dir, 'private', name);
    writeFileSync(path, 'x\n');
    if (mtime) utimesSync(path, mtime, mtime);
  }
}

const stop = (dir, active = false) => run('stop-reminder.mjs', { hook_event_name: 'Stop', stop_hook_active: active, cwd: dir });

test('stop reminder blocks once when code changed after private STATUS and LOG', () => {
  const dir = tempRepo();
  try {
    privateDocs(dir, HOUR_AGO);
    writeFileSync(join(dir, 'app.py'), 'print(1)\n');
    const r = stop(dir);
    assert.equal(r.output?.decision, 'block');
    assert.match(r.output.reason, /app\.py/);
    assert.match(r.output.reason, /private\/STATUS\.md/);
    assert.equal(stop(dir, true).output, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('stop reminder stays quiet when private STATUS and LOG were updated after the code', () => {
  const dir = tempRepo();
  try {
    writeFileSync(join(dir, 'app.py'), 'print(1)\n');
    utimesSync(join(dir, 'app.py'), HOUR_AGO, HOUR_AGO);
    privateDocs(dir);
    assert.equal(stop(dir).output, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('stop reminder stays quiet when only docs changed', () => {
  const dir = tempRepo();
  try {
    privateDocs(dir, HOUR_AGO);
    writeFileSync(join(dir, 'NOTES.md'), 'notes\n');
    assert.equal(stop(dir).output, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('stop reminder stays quiet when there is no private folder', () => {
  const dir = tempRepo();
  try {
    writeFileSync(join(dir, 'app.py'), 'print(1)\n');
    assert.equal(stop(dir).output, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// --- Private-words check before commits (D-031) ------------------------------

function repoWithWordList() {
  const dir = tempRepo();
  mkdirSync(join(dir, 'private'));
  writeFileSync(join(dir, 'private', 'pii-patterns.txt'), '# test list\nzanzibar\n');
  return dir;
}

const gitIn = (dir, ...args) => execFileSync('git', args, { cwd: dir, stdio: 'ignore' });

test('command guard blocks a commit whose staged changes contain a private word', () => {
  const dir = repoWithWordList();
  try {
    writeFileSync(join(dir, 'notes.txt'), 'Greetings from Zanzibar\n');
    gitIn(dir, 'add', 'notes.txt');
    const r = shell('git commit -m "add notes"', 'Bash', dir);
    assert.ok(isDenied(r), JSON.stringify(r.output));
    assert.match(r.output.hookSpecificOutput.permissionDecisionReason, /private details/);
    assert.match(r.output.hookSpecificOutput.permissionDecisionReason, /notes\.txt:1/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard blocks a commit that stages files under private/', () => {
  const dir = repoWithWordList();
  try {
    writeFileSync(join(dir, 'private', 'plan.md'), 'plan\n');
    gitIn(dir, 'add', '-f', 'private/plan.md');
    const r = shell('git commit -m "add plan"', 'Bash', dir);
    assert.ok(isDenied(r), JSON.stringify(r.output));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard checks the private words in the repo that git -C commit points at', () => {
  const dir = projectWithPrivateRepo();
  try {
    writeFileSync(join(dir, 'private', 'pii-patterns.txt'), '# test list\nzanzibar\n');
    writeFileSync(join(dir, 'notes.txt'), 'Greetings from Zanzibar\n');
    gitIn(dir, 'add', 'notes.txt');
    assert.ok(isDenied(shell('git commit -m "add notes"', 'Bash', dir)));
    assert.equal(shell('git -C private commit -m "add notes"', 'Bash', dir).output, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('command guard allows a commit with clean staged changes', () => {
  const dir = repoWithWordList();
  try {
    writeFileSync(join(dir, 'notes.txt'), 'nothing personal here\n');
    gitIn(dir, 'add', 'notes.txt');
    assert.equal(shell('git commit -m "add notes"', 'Bash', dir).output, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
