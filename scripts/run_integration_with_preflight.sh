#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/mfethe/openclaw-shared/workspace/fumemory"
cd "$ROOT"

# Railway-first binding with no localhost fallback in integration mode.
export NATS_URL="${NATS_URL:-${RAILWAY_NATS_URL:-}}"
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-${RAILWAY_TEMPORAL_ADDRESS:-}}"
export INTEGRATION_MODE=1

python3 scripts/run_pressure_test_pack.py
