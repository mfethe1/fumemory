#!/bin/bash
# memU Memory Agent — run compaction + concept indexing + distillation
# Designed to run when agents are idle (e.g., via cron at 2 AM, or OpenClaw heartbeat)
#
# Usage:
#   ./scripts/run_memory_agent.sh             # full run (all phases)
#   ./scripts/run_memory_agent.sh --compact   # compaction only
#   ./scripts/run_memory_agent.sh --index     # concept indexing only
#   ./scripts/run_memory_agent.sh --status    # show stats
#   ./scripts/run_memory_agent.sh --daemon    # run as persistent daemon

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="/tmp/memory_agent_$(date +%Y%m%d).log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting memory agent run: $*" | tee -a "$LOG_FILE"

# Source .env if it exists
if [ -f "$REPO_DIR/.env" ]; then
    export $(grep -v '^#' "$REPO_DIR/.env" | xargs) 2>/dev/null || true
fi

# Set defaults
export DATABASE_URL="${DATABASE_URL:-postgresql://memu:memu@localhost:5432/memu}"
export OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-qwen3-embedding}"
export EMBEDDING_DIMS="${EMBEDDING_DIMS:-4096}"
export CLUSTER_THRESHOLD="${CLUSTER_THRESHOLD:-0.82}"
export MIN_CLUSTER_SIZE="${MIN_CLUSTER_SIZE:-3}"

cd "$REPO_DIR"

# Default to --full if no args
ARGS="${@:---full}"

python3 -m memu.memory_agent $ARGS 2>&1 | tee -a "$LOG_FILE"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Memory agent run complete." | tee -a "$LOG_FILE"
