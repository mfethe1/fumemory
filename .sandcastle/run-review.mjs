import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { claudeCode, run } from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";

const repoDir = process.cwd();
const logDir = join(repoDir, ".sandcastle", "logs");
mkdirSync(logDir, { recursive: true });

const branch = process.argv[2];
if (!branch) {
  console.error("Usage: node .sandcastle/run-review.mjs <branch> [base-branch]");
  process.exit(1);
}

const baseBranch =
  process.argv[3] || process.env.SANDCASTLE_BASE_BRANCH || "sandcastle/openclaw-memory-prd-base";

function branchSlug(input) {
  return input.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80);
}

if (!process.env.ANTHROPIC_API_KEY) {
  console.error("ANTHROPIC_API_KEY is required for the default Claude Code Sandcastle runner.");
  process.exit(1);
}

try {
  execFileSync("git", ["rev-parse", "--verify", branch], {
    cwd: repoDir,
    stdio: ["ignore", "ignore", "pipe"],
  });
} catch {
  console.error(`Branch not found: ${branch}`);
  process.exit(1);
}

try {
  const result = await run({
    cwd: repoDir,
    agent: claudeCode("claude-sonnet-4-6", {
      env: { ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY },
    }),
    sandbox: docker({ imageName: "sandcastle:fumemory" }),
    branchStrategy: { type: "branch", branch, baseBranch },
    promptFile: resolve(repoDir, ".sandcastle", "review-prompt.md"),
    promptArgs: {
      BRANCH: branch,
      BASE_BRANCH: baseBranch,
    },
    hooks: {
      sandbox: {
        onSandboxReady: [
          {
            command:
              "git config core.filemode false && git config core.autocrlf false && git reset --hard HEAD && python3 -m venv .venv && . .venv/bin/activate && python -m pip install --disable-pip-version-check --upgrade pip && python -m pip install --disable-pip-version-check --prefer-binary -e \".[dev]\"",
            timeoutMs: 1800000,
          },
        ],
      },
    },
    logging: {
      type: "file",
      path: join(logDir, `review-${branchSlug(branch)}.log`),
    },
    maxIterations: 3,
    idleTimeoutSeconds: 900,
    name: `review-${branchSlug(branch).slice(0, 32)}`,
  });

  console.log(
    JSON.stringify(
      {
        branch: result.branch,
        commits: result.commits.map((commit) => commit.sha),
        logFilePath: result.logFilePath,
        completionSignal: result.completionSignal,
      },
      null,
      2,
    ),
  );
} catch (error) {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exit(1);
}
