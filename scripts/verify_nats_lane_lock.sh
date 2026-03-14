#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NATS_PORT="${NATS_TEST_PORT:-4422}"
MONITOR_PORT="${NATS_TEST_MONITOR_PORT:-8422}"
DATA_DIR="$ROOT/.local-nats-data/verify-baseline"
LOG_FILE="$ROOT/.local-nats.out"
CONF_FILE="$ROOT/infra/local-nats/nats-server.conf"
TMP_CONF="$ROOT/.local-nats-test.conf"

mkdir -p "$DATA_DIR"
sed "s#store_dir: /data/jetstream#store_dir: ${DATA_DIR}#" "$CONF_FILE" > "$TMP_CONF"

cleanup() {
  if [[ -n "${NATS_PID:-}" ]] && kill -0 "$NATS_PID" 2>/dev/null; then
    kill "$NATS_PID" 2>/dev/null || true
    wait "$NATS_PID" 2>/dev/null || true
  fi
  rm -f "$TMP_CONF"
}
trap cleanup EXIT

echo "==> Starting isolated JetStream NATS for lane-lock integration evidence"
nats-server \
  -c "$TMP_CONF" \
  -p "$NATS_PORT" \
  -m "$MONITOR_PORT" \
  --jetstream \
  >"$LOG_FILE" 2>&1 &
NATS_PID=$!

python3 - <<'PY' "$NATS_PORT" "$MONITOR_PORT"
import socket
import sys
import time

nats_port = int(sys.argv[1])
monitor_port = int(sys.argv[2])
deadline = time.time() + 15
ports = [nats_port, monitor_port]
while time.time() < deadline:
    ready = True
    for port in ports:
        sock = socket.socket()
        sock.settimeout(0.2)
        try:
            sock.connect(("127.0.0.1", port))
        except OSError:
            ready = False
        finally:
            sock.close()
    if ready:
        sys.exit(0)
    time.sleep(0.2)
print(f"Timed out waiting for NATS ports {ports} to open", file=sys.stderr)
sys.exit(1)
PY

echo "==> Running lane-lock integration test against JetStream NATS"
OUTPUT_FILE="$(mktemp)"
NATS_URL="nats://127.0.0.1:${NATS_PORT}" python3 -m pytest -q tests/test_lane_lock_claim_integration.py -rs | tee "$OUTPUT_FILE"

if ! grep -Eq '(^| )1 passed([, ]|$)' "$OUTPUT_FILE"; then
  echo "Lane-lock integration evidence gate failed: expected a real passing test run" >&2
  exit 1
fi

if grep -q 'SKIPPED' "$OUTPUT_FILE"; then
  echo "Lane-lock integration evidence gate failed: test skipped instead of passing" >&2
  exit 1
fi

echo "==> Lane-lock JetStream verification passed"
