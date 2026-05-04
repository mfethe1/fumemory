import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { claudeCode, run } from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";

const repoDir = process.cwd();
const logDir = join(repoDir, ".sandcastle", "logs");
const defaultBaseBranch = "sandcastle/openclaw-memory-prd-base";
mkdirSync(logDir, { recursive: true });

const args = process.argv.slice(2);
const force = args.includes("--force");
const dryRun = args.includes("--dry-run");
const issueArg = args.find((arg) => !arg.startsWith("--"));

if (!issueArg) {
  console.error("Usage: node .sandcastle/run-issue.mjs <issue-number> [--force] [--dry-run]");
  process.exit(1);
}

const issueNumber = issueArg.replace(/^#/, "");
const baseBranch = process.env.SANDCASTLE_BASE_BRANCH || defaultBaseBranch;

function gh(args) {
  return execFileSync("gh", args, {
    cwd: repoDir,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function git(args) {
  return execFileSync("git", args, {
    cwd: repoDir,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

function getIssue(number) {
  return JSON.parse(
    gh(["issue", "view", String(number), "--json", "number,title,body,url,state"]),
  );
}

function slug(input) {
  return input
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 56)
    .replace(/-$/g, "");
}

function blockerNumbers(body) {
  const match = /## Blocked by\s*([\s\S]*?)(?:\n## |\n# |$)/i.exec(body ?? "");
  if (!match) return [];

  const section = match[1];
  if (/none\s*-\s*can start immediately/i.test(section)) return [];
  if (/^none\b/im.test(section.trim())) return [];

  return [...section.matchAll(/#(\d+)/g)].map((m) => Number(m[1]));
}

function blockerCompletedLocally(number) {
  const branches = git(["branch", "--list", `sandcastle/issue-${number}-*`])
    .split("\n")
    .map((line) => line.replace(/^[*+\s]+/, "").trim().split(/\s+/)[0])
    .filter(Boolean);

  return branches.some((branch) => {
    try {
      execFileSync("git", ["merge-base", "--is-ancestor", branch, baseBranch], {
        cwd: repoDir,
        stdio: "ignore",
      });
      return true;
    } catch {
      return false;
    }
  });
}

function openBlockers(numbers) {
  return numbers.filter((number) => {
    try {
      return getIssue(number).state === "OPEN" && !blockerCompletedLocally(number);
    } catch {
      return !blockerCompletedLocally(number);
    }
  });
}

const issue = getIssue(issueNumber);
if (issue.state !== "OPEN") {
  console.log(`Issue #${issue.number} is ${issue.state}; nothing to run.`);
  process.exit(0);
}

const blockers = openBlockers(blockerNumbers(issue.body));
if (blockers.length && !force) {
  const message = `Issue #${issue.number} is blocked by open issue(s): ${blockers
    .map((n) => `#${n}`)
    .join(", ")}. Use --force to override.`;
  console.log(message);
  writeFileSync(join(logDir, `issue-${issue.number}-blocked.log`), `${message}\n`);
  process.exit(2);
}

const branch = process.env.SANDCASTLE_BRANCH || `sandcastle/issue-${issue.number}-${slug(issue.title)}`;
const logPath = join(logDir, `issue-${issue.number}-${slug(issue.title)}.log`);
const maxIterations = Number(process.env.SANDCASTLE_MAX_ITERATIONS || "8");
const idleTimeoutSeconds = Number(process.env.SANDCASTLE_IDLE_TIMEOUT_SECONDS || "900");

if (dryRun) {
  console.log(
    JSON.stringify(
      {
        issue: issue.number,
        title: issue.title,
        branch,
        baseBranch,
        blockers,
        logPath,
        maxIterations,
        idleTimeoutSeconds,
      },
      null,
      2,
    ),
  );
  process.exit(0);
}

if (!process.env.ANTHROPIC_API_KEY) {
  console.error("ANTHROPIC_API_KEY is required for the default Claude Code Sandcastle runner.");
  process.exit(1);
}

const setupCommand = [
  "git config core.filemode false",
  "git config core.autocrlf false",
  "git reset --hard HEAD",
  "python3 -c \"import memu, pytest\"",
].join(" && ");

try {
  const result = await run({
    cwd: repoDir,
    agent: claudeCode("claude-sonnet-4-6", {
      env: { ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY },
    }),
    sandbox: docker({ imageName: "sandcastle:fumemory" }),
    branchStrategy: { type: "branch", branch, baseBranch },
    promptFile: resolve(repoDir, ".sandcastle", "issue-prompt.md"),
    promptArgs: {
      ISSUE_NUMBER: String(issue.number),
      ISSUE_TITLE: issue.title,
      ISSUE_URL: issue.url,
      ISSUE_BODY: issue.body ?? "",
      BRANCH: branch,
    },
    hooks: {
      sandbox: {
        onSandboxReady: [{ command: setupCommand, timeoutMs: 1800000 }],
      },
    },
    logging: { type: "file", path: logPath },
    maxIterations,
    idleTimeoutSeconds,
    name: `issue-${issue.number}`,
  });

  console.log(
    JSON.stringify(
      {
        issue: issue.number,
        branch: result.branch,
        commits: result.commits.map((commit) => commit.sha),
        logFilePath: result.logFilePath,
        completionSignal: result.completionSignal,
        preservedWorktreePath: result.preservedWorktreePath,
      },
      null,
      2,
    ),
  );
} catch (error) {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exit(1);
}
