/** Dependency-free deterministic Gates for the TypeScript Fixture. */

import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

const gate = process.argv[2];
const compilerNode = process.argv[3];
const compilerJs = process.argv[4];
const output = join(process.env.TMPDIR, `phase6-ts-${process.pid}`);
mkdirSync(output, { recursive: true });

function compile(noEmit = false) {
  const args = [
    "--strict",
    "--target",
    "ES2020",
    "--module",
    "commonjs",
    "--skipLibCheck",
  ];
  if (noEmit) {
    args.push("--noEmit");
  } else {
    args.push("--outDir", output);
  }
  args.push("tag_normalizer.ts");
  return spawnSync(compilerNode, [compilerJs, ...args], {
    encoding: "utf8",
    env: process.env,
    shell: false,
  });
}

function loadImplementation() {
  const compiled = compile(false);
  if (compiled.status !== 0) {
    return null;
  }
  const require = createRequire(import.meta.url);
  return require(join(output, "tag_normalizer.js")).normalizeTags;
}

function acceptance() {
  const normalizeTags = loadImplementation();
  if (normalizeTags === null) {
    return false;
  }
  const actual = normalizeTags([
    "  Hello World  ",
    "hello__world",
    "Alpha_beta",
    " --Trim-- ",
    "___",
    "ALPHA BETA",
    "x   y",
    "a- _b",
  ]);
  return JSON.stringify(actual) === JSON.stringify([
    "hello-world",
    "alpha-beta",
    "trim",
    "x-y",
    "a--b",
  ]);
}

function regression() {
  const normalizeTags = loadImplementation();
  return normalizeTags !== null
    && JSON.stringify(normalizeTags([])) === "[]"
    && JSON.stringify(normalizeTags(["plain-tag"])) === '["plain-tag"]';
}

function lint() {
  const content = readFileSync("tag_normalizer.ts", "utf8");
  return content.includes(
    "export function normalizeTags(tags: string[]): string[]",
  ) && !content.includes("\t")
    && content.split("\n").every((line) => line === line.trimEnd());
}

function typecheck() {
  return compile(true).status === 0;
}

const gates = { acceptance, regression, lint, typecheck };
if (!(gate in gates)) {
  process.exit(2);
}
process.exit(gates[gate]() ? 0 : 1);
