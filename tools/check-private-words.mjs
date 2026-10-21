// Private-words check (D-031): keeps the owner's personal details out of this public repo.
// The word list lives in private/pii-patterns.txt, which is never committed here.
//   node tools/check-private-words.mjs --staged   changes about to be committed (staged and unstaged tracked changes)
//   node tools/check-private-words.mjs --all      every file that would be committed (tracked and new, not ignored)
// Exit 0 = clean (or no word list on this machine), 1 = private words or private/ paths found, 2 = usage error.
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const mode = process.argv[2];
if (mode !== '--staged' && mode !== '--all') {
  console.error('usage: node tools/check-private-words.mjs --staged | --all');
  process.exit(2);
}

const git = (args) => execFileSync('git', args, { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
const root = git(['rev-parse', '--show-toplevel']).trim();
const listFile = join(root, 'private', 'pii-patterns.txt');
if (!existsSync(listFile)) {
  console.log('check-private-words: no private/pii-patterns.txt on this machine, skipped.');
  process.exit(0);
}

// Plain-text words, matched case-insensitively. Findings name the word by its number, not its text.
const words = readFileSync(listFile, 'utf8')
  .split(/\r?\n/)
  .map((line) => line.trim())
  .filter((line) => line && !line.startsWith('#'))
  .map((word) => word.toLowerCase());

const findings = new Set();
function scan(where, text) {
  const lower = text.toLowerCase();
  words.forEach((word, i) => {
    if (lower.includes(word)) findings.add(`${where}: matches private word #${i + 1}`);
  });
}
function checkPath(path) {
  if (path.startsWith('private/')) findings.add(`${path}: files under private/ must never be committed here`);
  scan(`${path} (file name)`, path);
}

if (mode === '--staged') {
  for (const path of git(['diff', '--cached', '--name-only']).split('\n').filter(Boolean)) checkPath(path);
  // Added lines only: staged changes, plus unstaged tracked changes that `git commit -a` would include.
  for (const args of [['diff', '--cached', '-U0', '--no-color'], ['diff', '-U0', '--no-color']]) {
    let file = '';
    let line = 0;
    for (const text of git(args).split('\n')) {
      if (text.startsWith('+++ ')) { file = text.replace(/^\+\+\+ (b\/)?/, ''); continue; }
      const hunk = text.match(/^@@ -\d+(?:,\d+)? \+(\d+)/);
      if (hunk) { line = Number(hunk[1]); continue; }
      if (text.startsWith('+')) { scan(`${file}:${line}`, text.slice(1)); line += 1; }
    }
  }
} else {
  for (const path of git(['ls-files', '--cached', '--others', '--exclude-standard']).split('\n').filter(Boolean)) {
    checkPath(path);
    let content;
    try { content = readFileSync(join(root, path), 'utf8'); } catch { continue; }
    if (content.includes('\u0000')) continue; // binary file
    content.split(/\r?\n/).forEach((text, i) => scan(`${path}:${i + 1}`, text));
  }
}

if (findings.size) {
  console.log('check-private-words: blocked. Private details found (word list: private/pii-patterns.txt):');
  for (const finding of findings) console.log(`  ${finding}`);
  process.exit(1);
}
console.log(`check-private-words: clean (${words.length} words checked).`);
