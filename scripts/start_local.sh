#!/bin/bash
set -e
echo "🔄 Starting memU locally..."
cd /Users/mfethe/openclaw-shared/workspace/fumemory

# Kill existing uvicorn processes safely
if pgrep -f "uvicorn memu.api:app" > /dev/null; then
    echo "🛑 Killing existing memU instances..."
    pkill -f "uvicorn memu.api:app"
    sleep 2
fi

# Apply migrations
echo "📦 Running database migrations..."
# Assuming uv run handles the migrations or it runs on app start.

# Start API in background
echo "🚀 Starting uvicorn in background..."
nohup uv run uvicorn memu.api:app --host 0.0.0.0 --port 8000 > memu_api.log 2>&1 &
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
