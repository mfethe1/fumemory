#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Testing local NATS config syntax"
nats-server -t -c infra/local-nats/nats-server.conf

echo "==> Running pytest"
python3 -m pytest -q

echo "==> Baseline verification passed"
