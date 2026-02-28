#!/bin/bash
set -e

echo "🚀 Deploying fumemory/memU stack..."

# Pull latest code
git pull origin main

# Build and start containers
echo "📦 Building containers..."
docker compose up -d --build

# Wait for health check
echo "⏳ Waiting for API to be healthy..."
max_retries=30
count=0
while [ $count -lt $max_retries ]; do
    if curl -s http://localhost:8000/health | grep "ok" > /dev/null; then
        echo "✅ memU is LIVE!"
        exit 0
    fi
    echo "   ... waiting ($count/$max_retries)"
    sleep 2
    count=$((count + 1))
done

echo "❌ Deployment timed out waiting for health check."
exit 1
