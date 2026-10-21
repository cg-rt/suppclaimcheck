// Shared helpers for the Claude Code guard hooks (P0.1).
// Claude Code passes each hook a JSON description of the event on stdin.
import { readFileSync } from 'node:fs';

export function readHookInput() {
  return JSON.parse(readFileSync(0, 'utf8'));
}

// PreToolUse only: block the tool call. The reason is shown to the agent.
export function deny(reason) {
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason,
    },
  }));
  process.exit(0);
}

// Paths that hold secrets. Mirrors the secret section of .gitignore.
const SECRET_RULES = [
  { pattern: /(^|\/)\.env(?!\.example$)(\.[^/]+)?$/, why: '.env files hold secrets' },
  { pattern: /(^|\/)secrets(\/|$)/, why: 'secrets/ holds Docker secret files' },
  { pattern: /\.(pem|key|p12|pfx)$/, why: 'key and certificate files are secrets' },
  { pattern: /(^|\/)restic-password[^/]*$/, why: 'the backup password is a secret' },
  { pattern: /(^|\/)[^/]*credentials[^/]*\.json$/, why: 'credential files are secrets' },
  { pattern: /(^|\/)\.ssh(\/|$)/, why: 'SSH keys belong to the owner' },
];

// Returns why a path is secret, or null. Works for Windows and POSIX paths.
export function secretReason(path) {
  const normalized = String(path).replace(/\\/g, '/').toLowerCase();
  for (const rule of SECRET_RULES) {
    if (rule.pattern.test(normalized)) return rule.why;
  }
  return null;
}
