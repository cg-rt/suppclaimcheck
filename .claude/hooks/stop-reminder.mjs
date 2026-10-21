// Stop hook: before the agent finishes, remind it to update private/STATUS.md and private/LOG.md when code changed (P0.1, D-031).
// A reminder, not a security control: on any error, or when private/ is absent, it lets the agent stop.
import { execFileSync } from 'node:child_process';
import { existsSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { readHookInput } from './lib.mjs';

const REQUIRED_DOCS = ['private/STATUS.md', 'private/LOG.md'];

function git(args, cwd) {
  return execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
}

function changedFiles(cwd) {
  const files = new Set();
  // Uncommitted changes, including new files. Porcelain paths are relative to the repo root.
  for (const line of git(['status', '--porcelain', '--untracked-files=all'], cwd).split('\n')) {
    let path = line.slice(3).trim();
    if (path.includes(' -> ')) path = path.split(' -> ')[1];
    path = path.replace(/^"|"$/g, '');
    if (path) files.add(path);
  }
  // Commits on a feature branch that main doesn't have yet.
  if (git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd).trim() !== 'main') {
    for (const path of git(['diff', '--name-only', 'main...HEAD'], cwd).split('\n')) {
      if (path.trim()) files.add(path.trim());
    }
  }
  return [...files];
}

try {
  const input = readHookInput();
  // Already reminded once in this stop sequence: let the agent stop.
  if (!input.stop_hook_active) {
    const root = git(['rev-parse', '--show-toplevel'], input.cwd || process.env.CLAUDE_PROJECT_DIR).trim();
    if (existsSync(join(root, 'private'))) {
      const code = changedFiles(root).filter((f) => !f.startsWith('docs/') && !f.startsWith('private/') && !f.endsWith('.md'));
      const mtimes = code.map((f) => join(root, f)).filter(existsSync).map((p) => statSync(p).mtimeMs);
      const newestCode = Math.max(0, ...mtimes);
      // A doc counts as updated if it was saved after the newest changed code file.
      const stale = REQUIRED_DOCS.filter((d) => {
        const p = join(root, d);
        return !existsSync(p) || statSync(p).mtimeMs < newestCode;
      });
      if (code.length && stale.length) {
        const shown = code.slice(0, 5).join(', ') + (code.length > 5 ? `, and ${code.length - 5} more` : '');
        process.stdout.write(JSON.stringify({
          decision: 'block',
          reason: `Code changed (${shown}) but ${stale.join(' and ')} not updated since. `
            + 'Follow the end-of-session steps in AGENTS.md section 5. If the work is not finished yet, say so and stop again.',
        }));
      }
    }
  }
} catch {
  // Never trap the agent because of a reminder.
}
process.exit(0);
