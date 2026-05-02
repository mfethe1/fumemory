#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "🔄 Starting memU locally..."
cd "$REPO_DIR"

if [ -f .env ]; then
  # shellcheck disable=SC1091
  source .env
fi
if [ -z "${MEMU_API_KEY:-}" ] && [ -f "$HOME/.openclaw/secrets/memu_api_key" ]; then
  export MEMU_API_KEY="$(tr -d '\r\n' < "$HOME/.openclaw/secrets/memu_api_key")"
fi
if [ -z "${MEMU_API_KEY:-}" ] && [ -n "${MEMU_SHARED_SECRET:-}" ]; then
  export MEMU_API_KEY="$MEMU_SHARED_SECRET"
fi

# Kill existing uvicorn processes safely
if pgrep -f "memu.api:app" > /dev/null; then
    echo "🛑 Killing existing memU instances..."
    pkill -f "memu.api:app" || true
    sleep 2
fi
if lsof -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "🧹 Clearing stale listeners on port 8000..."
    lsof -tiTCP:8000 -sTCP:LISTEN | xargs kill -9 2>/dev/null || true
    sleep 1
fi

# Apply migrations
echo "📦 Running database migrations..."
# Assuming uv run handles the migrations or it runs on app start.

# Start API in background
echo "🚀 Starting uvicorn in background..."
nohup env MEMU_API_KEY="${MEMU_API_KEY:-}" uv run uvicorn memu.api:app --host 0.0.0.0 --port 8000 > memu_api.log 2>&1 &
echo $! > memu.pid

echo "⏳ Waiting for health check..."
sleep 3
if curl -s http://localhost:8000/health | grep "healthy" > /dev/null; then
    echo "✅ memU is LIVE at http://localhost:8000"
    exit 0
else
    echo "❌ Failed to start. Checking logs:"
    tail -n 10 memu_api.log
    exit 1
fi
