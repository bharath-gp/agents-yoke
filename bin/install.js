#!/usr/bin/env node
// Installs the yoke CLI and its plugin into Claude Code and/or Antigravity (agy).
// Usage: npx github:bharath-gp/agents-yoke [--scope user|project|local] [--only claude,agy] [--no-cli] [--dry-run]
"use strict";
const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

const REPO = "bharath-gp/agents-yoke";
const GIT_URL = `https://github.com/${REPO}.git`;
const PLUGIN_DIR = path.join(os.homedir(), ".yoke", "plugin"); // agy installs from a directory
const SCOPES = ["user", "project", "local"];

const args = process.argv.slice(2);
const dry = args.includes("--dry-run");
const noCli = args.includes("--no-cli");
const opt = (name) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] || "" : null;
};
const only = opt("--only") !== null ? opt("--only").split(",") : null;

if (args.includes("-h") || args.includes("--help")) {
  console.log("usage: yoke-install [--scope user|project|local] [--only claude,agy] [--no-cli] [--dry-run]");
  process.exit(0);
}

const has = (cmd) => spawnSync("sh", ["-c", `command -v ${cmd}`]).status === 0;

// Returns true on exit 0. Output streams to the terminal.
function run(cmd, cmdArgs) {
  console.log(`$ ${[cmd, ...cmdArgs].join(" ")}`);
  if (dry) return true;
  return spawnSync(cmd, cmdArgs, { stdio: "inherit" }).status === 0;
}

function ask(q) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((res) => rl.question(q, (a) => { rl.close(); res(a.trim()); }));
}

async function pickScope() {
  const given = opt("--scope");
  if (given !== null) {
    if (!SCOPES.includes(given)) {
      console.error(`--scope must be one of ${SCOPES.join(", ")}`);
      process.exit(1);
    }
    return given;
  }
  if (!process.stdin.isTTY) return "user";
  const a = await ask("Install for (1) you, all projects  (2) this folder/repo only, shared via git  (3) this folder only, not shared [1]: ");
  return { 2: "project", 3: "local" }[a] || "user";
}

(async () => {
  let failed = false;
  const step = (ok, what) => {
    if (!ok) {
      failed = true;
      console.error(`! failed: ${what}`);
    }
  };

  if (!has("git")) {
    console.error("git is required");
    process.exit(1);
  }

  const scope = await pickScope();
  if (scope !== "user") console.log(`Scope ${scope}: installing into ${process.cwd()}`);

  if (!noCli) {
    console.log("\n== yoke CLI (always user-wide)");
    if (has("uv")) step(run("uv", ["tool", "install", "--force", `git+${GIT_URL}`]), "uv tool install");
    else if (has("pipx")) step(run("pipx", ["install", "--force", `git+${GIT_URL}`]), "pipx install");
    else {
      console.error("! need uv (https://docs.astral.sh/uv) or pipx to install the yoke CLI; rerun after installing one");
      failed = true;
    }
  }

  const targets = ["claude", "agy"].filter((t) => (only ? only.includes(t) : has(t)));
  if (!targets.length) console.log("\nNo claude or agy on PATH (or none selected by --only); CLI only.");

  if (targets.includes("claude")) {
    console.log("\n== Claude Code");
    step(run("claude", ["plugin", "marketplace", "add", REPO, "--scope", scope]), "claude marketplace add");
    step(run("claude", ["plugin", "install", "yoke@yoke", "--scope", scope]), "claude plugin install");
  }

  if (targets.includes("agy")) {
    console.log("\n== Antigravity (agy)");
    // ponytail: agy plugin install has no scope option, so it is always user-wide
    if (scope !== "user") console.log("note: agy installs plugins user-wide only; scope ignored for agy");
    let ok = true;
    if (fs.existsSync(path.join(PLUGIN_DIR, ".git"))) ok = run("git", ["-C", PLUGIN_DIR, "pull", "--ff-only"]);
    else {
      if (!dry) fs.mkdirSync(path.dirname(PLUGIN_DIR), { recursive: true });
      ok = run("git", ["clone", "--depth", "1", GIT_URL, PLUGIN_DIR]);
    }
    step(ok, "fetch plugin source");
    if (ok) step(run("agy", ["plugin", "install", PLUGIN_DIR]), "agy plugin install");
  }

  console.log(failed ? "\nDone with errors." : "\nDone. In your agent, run /yoke:setup (agy: the setup skill).");
  process.exit(failed ? 1 : 0);
})();
