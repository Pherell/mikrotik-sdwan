/**
 * Guards against the CSS mistakes that have actually shipped here.
 *
 * `tsc` and `vite build` both pass on a button rendered 67 pixels wide across
 * three lines, because neither computes layout. Catching that in a test would
 * need a real browser engine -- jsdom has no layout at all, so every
 * getBoundingClientRect() there returns zeros. A headless browser in CI is a
 * lot of machinery to detect one literal string, so this checks the string.
 *
 * It catches the cause rather than the symptom, which is the more useful half:
 * the symptom had one cause, repeated by copy-paste 44 times.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SRC = join(ROOT, "src");

const RULES = [
  {
    // style={{ flex: 0 }} and style={{ flex: 0, ... }}
    pattern: /\bflex:\s*(0|"0")\s*[,}]/,
    name: "flex: 0",
    why: [
      "`flex: 0` is shorthand for `flex: 0 1 0%` -- basis zero, and shrinking",
      "still ALLOWED. The item collapses to the widest unbreakable word inside",
      "it. This is why every primary button in this app once rendered ~67px",
      'wide over two or three lines; "Check all for drift" came to rest at the',
      'width of "Check".',
      "",
      'Use className="no-grow" (or `flex: none`, which is `0 0 auto`).',
    ],
  },
  {
    // A button that can shrink below its label is the same bug wearing a hat.
    pattern: /\bflexShrink:\s*1\b/,
    name: "flexShrink: 1 on a control",
    why: [
      "Controls size to their label. If one needs to shrink, give it an",
      "explicit width and an overflow rule instead.",
    ],
  },
];

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (/\.(tsx?|css)$/.test(entry)) out.push(full);
  }
  return out;
}

const findings = [];
for (const file of walk(SRC)) {
  const lines = readFileSync(file, "utf8").split(/\r?\n/);
  lines.forEach((line, i) => {
    for (const rule of RULES) {
      if (rule.pattern.test(line)) {
        findings.push({ file: relative(ROOT, file), line: i + 1, text: line.trim(), rule });
      }
    }
  });
}

if (findings.length === 0) {
  console.log("check-styles: clean");
  process.exit(0);
}

const byRule = new Map();
for (const f of findings) {
  if (!byRule.has(f.rule.name)) byRule.set(f.rule.name, []);
  byRule.get(f.rule.name).push(f);
}

for (const [name, hits] of byRule) {
  console.error(`\n${name} -- ${hits.length} occurrence${hits.length > 1 ? "s" : ""}\n`);
  for (const line of hits[0].rule.why) console.error(`  ${line}`);
  console.error("");
  for (const h of hits) console.error(`  ${h.file}:${h.line}: ${h.text}`);
}
console.error("");
process.exit(1);
