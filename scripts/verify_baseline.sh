#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Testing local NATS config syntax"
nats-server -t -c infra/local-nats/nats-server.conf

echo "==> Running JetStream-backed lane-lock verification"
./scripts/verify_nats_lane_lock.sh

echo "==> Running remaining pytest suite"
python3 -m pytest -q tests --ignore=tests/test_lane_lock_claim_integration.py

echo "==> Baseline verification passed"
