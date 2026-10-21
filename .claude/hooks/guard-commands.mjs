// PreToolUse guard for shell commands: blocks the never-list commands (P0.1, AGENTS.md section 6).
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readHookInput, deny, secretReason } from './lib.mjs';

// The private-words scanner lives in the same checkout as this hook (D-031).
const SCANNER = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'tools', 'check-private-words.mjs');

// One command segment: stops at a newline or a shell separator.
const SEG = String.raw`[^\n;&|]*`;

const COMMAND_RULES = [
  {
    pattern: new RegExp(String.raw`\bgit\b${SEG}\bpush\b${SEG}(\s--force(-with-lease|-if-includes)?\b|\s-[a-zA-Z]*f[a-zA-Z]*\b|\s\+\S)`),
    why: 'force-pushing rewrites shared history',
  },
  {
    pattern: new RegExp(String.raw`\bgit\b${SEG}\bpush\b${SEG}[\s:](main|master)\b`),
    why: 'main only changes through pull requests',
  },
  {
    pattern: new RegExp(String.raw`\bgit\b${SEG}\s--no-verify\b`),
    why: '--no-verify skips the safety checks',
  },
  {
    pattern: new RegExp(String.raw`\bgit\b${SEG}\bcommit\b${SEG}\s-[a-zA-Z]*n[a-zA-Z]*\b`),
    why: 'git commit -n skips the safety checks',
  },
  {
    pattern: new RegExp(String.raw`(^|[\s;&|])SKIP=\S*${SEG}\bgit\b${SEG}\bcommit\b`),
    why: 'SKIP= turns off pre-commit checks',
  },
  {
    pattern: /\$env:SKIP\s*=/i,
    why: 'SKIP turns off pre-commit checks',
  },
  {
    pattern: new RegExp(String.raw`\bdocker(-compose|\s+compose)\b${SEG}\bdown\b${SEG}\s(-v|--volumes)\b`),
    why: 'it deletes data volumes, where the database lives',
  },
  {
    pattern: /\bdocker\s+volume\s+(rm|remove|prune)\b/,
    why: 'it deletes data volumes, where the database lives',
  },
  {
    pattern: new RegExp(String.raw`\bdocker\s+system\s+prune\b${SEG}--volumes\b`),
    why: 'it deletes data volumes, where the database lives',
  },
  {
    pattern: new RegExp(String.raw`\bgit\b${SEG}\bclean\b${SEG}\s-[a-zA-Z]*[xX]`),
    why: 'git clean -x deletes ignored files, such as local secrets and the planning brief',
  },
  {
    pattern: /(^|[\s;&|(])(ssh|scp|sftp)(\.exe)?(\s|$)/i,
    why: 'the production VM is run by the owner; prepare the command and hand it over',
  },
];

// Heredoc and here-string bodies are data (commit messages, file contents), not commands.
function stripBodies(command) {
  return command
    .replace(/<<-?\s*(['"]?)(\w+)\1[^\n]*\n[\s\S]*?\n\s*\2(?=\s|$)/g, '<<$2')
    .replace(/@'[\s\S]*?'@/g, "''")
    .replace(/@"[\s\S]*?"@/g, '""');
}

// Quoted text (commit messages, search patterns) is not checked for flags.
function stripQuoted(command) {
  return command.replace(/'[^']*'|"(?:[^"\\]|\\.)*"/g, '""');
}

function currentBranch(cwd) {
  try {
    return execFileSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], {
      cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return null;
  }
}

// Runs the private-words check on what a commit would include. Returns its findings, or null when clean or not applicable.
function privateWordsFindings(cwd) {
  let root;
  try {
    root = execFileSync('git', ['rev-parse', '--show-toplevel'], {
      cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return null;
  }
  if (!existsSync(join(root, 'private', 'pii-patterns.txt')) || !existsSync(SCANNER)) return null;
  const result = spawnSync(process.execPath, [SCANNER, '--staged'], { cwd: root, encoding: 'utf8' });
  return result.status === 1 ? result.stdout.trim() : null;
}

try {
  const input = readHookInput();
  const body = stripBodies(String(input.tool_input?.command ?? ''));
  const flags = stripQuoted(body);

  for (const rule of COMMAND_RULES) {
    if (rule.pattern.test(flags)) deny(`Blocked by project hook: ${rule.why}. See AGENTS.md section 6.`);
  }

  if (/\bgit\b[^\n;&|]*\bpush\b/.test(flags)
      && currentBranch(input.cwd || process.env.CLAUDE_PROJECT_DIR) === 'main') {
    deny('Blocked by project hook: you are on main, which only changes through pull requests. Switch to a feature branch.');
  }

  if (/\bgit\b[^\n;&|]*\bcommit\b/.test(flags)) {
    const findings = privateWordsFindings(input.cwd || process.env.CLAUDE_PROJECT_DIR);
    if (findings) deny(`Blocked by project hook: the commit would publish private details (D-031).\n${findings}`);
  }

  // Paths are checked with quotes kept, because paths are often quoted.
  for (const token of body.split(/[\s;&|()<>,=]+/)) {
    const path = token.replace(/^['"`]+/, '').replace(/['"`.,:]+$/, '');
    if (!path) continue;
    const why = secretReason(path);
    if (why) deny(`Blocked by project hook: ${why} (${path}). Secrets stay with the owner; see AGENTS.md section 6.`);
  }
} catch (err) {
  // Fail closed: a broken guard must not silently let everything through.
  deny(`Blocked: the command guard failed (${err.message}). Fix .claude/hooks before continuing.`);
}
