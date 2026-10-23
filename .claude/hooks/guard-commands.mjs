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

// Like stripQuoted, but keeps a quoted path straight after -C, because it names the repo git acts on.
function stripQuotedExceptRepoPaths(command) {
  return command.replace(/(-C\s+)?('[^']*'|"(?:[^"\\]|\\.)*")/g, (match, option) => (option ? match : '""'));
}

const unquote = (token) => token.replace(/^(['"])(.*)\1$/, '$2');

// For each `git … <verb>` in the command, the -C folders given before the verb, in order.
// Git applies them one after another from the session folder; no -C means the session folder itself.
function gitRepoOptions(body, verb) {
  const found = [];
  for (const segment of stripQuotedExceptRepoPaths(body).split(/[\n;&|]+/)) {
    if (!new RegExp(String.raw`\bgit\b.*\b${verb}\b`).test(segment)) continue;
    const tokens = segment.match(/'[^']*'|"[^"]*"|\S+/g) ?? [];
    const dirs = [];
    for (let i = tokens.findIndex((t) => /^(\$?\()?git(\.exe)?$/i.test(t)) + 1; i > 0 && i < tokens.length && tokens[i].startsWith('-'); i++) {
      if (tokens[i] === '-C') dirs.push(unquote(tokens[++i] ?? ''));
      else if (tokens[i].startsWith('-C')) dirs.push(unquote(tokens[i].slice(2)));
      else if (tokens[i] === '-c') i++;
    }
    found.push(dirs);
  }
  // Fail closed: a git command the parser can't read is checked in the session folder.
  return found.length ? found : [[]];
}

// Git Bash writes D:\x as /d/x and converts it for git; Node passes paths to git as they are.
function nativePath(path) {
  return process.platform === 'win32' ? path.replace(/^\/([a-zA-Z])(?=\/|$)/, '$1:') : path;
}

// Runs git where the command would run it. Returns the output, or null when that is not a git repo.
function gitIn(cwd, dirs, args) {
  try {
    return execFileSync('git', [...dirs.flatMap((dir) => ['-C', nativePath(dir)]), ...args], {
      cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return null;
  }
}

const samePath = (a, b) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;

// The private notes repo (D-031) is its own repo at private/ in the project, with no pull requests: its main is pushed directly.
function isPrivateNotesRepo(top, projectDir) {
  if (!top || !projectDir) return false;
  const projectTop = gitIn(projectDir, [], ['rev-parse', '--show-toplevel']);
  const privateTop = gitIn(join(projectDir, 'private'), [], ['rev-parse', '--show-toplevel']);
  return privateTop !== null && projectTop !== null && !samePath(privateTop, projectTop) && samePath(top, privateTop);
}

// Runs the private-words check on what a commit would include. Returns its findings, or null when clean or not applicable.
function privateWordsFindings(cwd, dirs) {
  const root = gitIn(cwd, dirs, ['rev-parse', '--show-toplevel']);
  if (root === null) return null;
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

  const cwd = input.cwd || process.env.CLAUDE_PROJECT_DIR;
  const projectDir = process.env.CLAUDE_PROJECT_DIR || input.cwd;

  if (/\bgit\b[^\n;&|]*\bpush\b/.test(flags)) {
    for (const dirs of gitRepoOptions(body, 'push')) {
      const top = gitIn(cwd, dirs, ['rev-parse', '--show-toplevel']);
      if (dirs.length && top === null) {
        deny('Blocked by project hook: the push names a folder with -C that is not a git repo, so its branch could not be checked.');
      }
      if (gitIn(cwd, dirs, ['rev-parse', '--abbrev-ref', 'HEAD']) === 'main' && !isPrivateNotesRepo(top, projectDir)) {
        deny('Blocked by project hook: the repo this push runs in is on main, which only changes through pull requests. Switch to a feature branch.');
      }
    }
  }

  if (/\bgit\b[^\n;&|]*\bcommit\b/.test(flags)) {
    for (const dirs of gitRepoOptions(body, 'commit')) {
      const findings = privateWordsFindings(cwd, dirs);
      if (findings) deny(`Blocked by project hook: the commit would publish private details (D-031).\n${findings}`);
    }
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
