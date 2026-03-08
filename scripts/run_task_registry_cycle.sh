#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

: "${TASK_REGISTRY_TENANT_ID:=00000000-0000-0000-0000-000000000001}"
: "${TASK_REGISTRY_REPOS:?Set TASK_REGISTRY_REPOS as comma-separated paths}"
: "${TASK_REGISTRY_MENU_BUCKET:=code-scan}"
: "${TASK_REGISTRY_DB_URL=${DATABASE_URL:-}}"
: "${TASK_REGISTRY_OWNER:=system}"
: "${TASK_REGISTRY_INCLUDE:=.py,.ts,.tsx,.js,.md,.sh}"
: "${TASK_REGISTRY_EXCLUDE:=__pycache__,.pytest_cache,.venv,node_modules,.next,dist,build,target}"
: "${TASK_REGISTRY_RUN_REFINER:=1}"
: "${TASK_REGISTRY_REFINER_LIMIT:=200}"
: "${TASK_REGISTRY_REFINER_PUBLISH_NATS:=0}"
: "${TASK_REGISTRY_REFINER_OWNER:=}"
: "${TASK_REGISTRY_PUBLISH_NATS:=0}"
: "${TASK_REGISTRY_GITHUB_SYNC:=0}"
: "${TASK_REGISTRY_GITHUB_REPO:=}"

if [[ -z "$TASK_REGISTRY_DB_URL" ]]; then
  echo "DATABASE_URL not set and TASK_REGISTRY_DB_URL not provided" >&2
  exit 2
fi

SCAN_ARGS=(
  --repos "$TASK_REGISTRY_REPOS"
  --tenant-id "$TASK_REGISTRY_TENANT_ID"
  --owner "$TASK_REGISTRY_OWNER"
  --menu-bucket "$TASK_REGISTRY_MENU_BUCKET"
  --include "$TASK_REGISTRY_INCLUDE"
  --exclude "$TASK_REGISTRY_EXCLUDE"
  --db-url "$TASK_REGISTRY_DB_URL"
)

if [[ "$TASK_REGISTRY_PUBLISH_NATS" == "1" ]]; then
  SCAN_ARGS+=(--publish-nats)
fi

if [[ "$TASK_REGISTRY_GITHUB_SYNC" == "1" && -n "$TASK_REGISTRY_GITHUB_REPO" ]]; then
  SCAN_ARGS+=(--github-sync --github-repo "$TASK_REGISTRY_GITHUB_REPO")
fi

echo "[task-registry] running scanner"
python3 scripts/task_registry_scanner.py "${SCAN_ARGS[@]}"

if [[ "$TASK_REGISTRY_RUN_REFINER" == "1" ]]; then
  REF_ARGS=(--db-url "$TASK_REGISTRY_DB_URL" --limit "$TASK_REGISTRY_REFINER_LIMIT")

  if [[ -n "$TASK_REGISTRY_REFINER_OWNER" ]]; then
    REF_ARGS+=(--owner "$TASK_REGISTRY_REFINER_OWNER")
  fi

  if [[ "$TASK_REGISTRY_REFINER_PUBLISH_NATS" == "1" ]]; then
    REF_ARGS+=(--publish-nats)
  fi

  echo "[task-registry] running refiner"
  python3 scripts/task_refiner_agent.py "${REF_ARGS[@]}"
fi

echo "[task-registry] cycle complete"
