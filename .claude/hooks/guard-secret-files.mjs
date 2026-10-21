// PreToolUse guard for the file tools: secret files stay out of the agent's view (P0.1, AGENTS.md section 6).
import { readHookInput, deny, secretReason } from './lib.mjs';

try {
  const input = readHookInput();
  const toolInput = input.tool_input ?? {};
  const target = toolInput.file_path ?? toolInput.notebook_path ?? toolInput.path;
  if (target) {
    const why = secretReason(target);
    if (why) {
      deny(`Blocked by project hook: ${why} (${target}). Secrets stay with the owner; see AGENTS.md section 6.`);
    }
  }
} catch (err) {
  // Fail closed: a broken guard must not silently let everything through.
  deny(`Blocked: the secret-file guard failed (${err.message}). Fix .claude/hooks before continuing.`);
}
