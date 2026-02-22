#!/usr/bin/env bash
# Setup script for local NATS instance (primary node)
# Downloads NATS server binary and starts with JetStream enabled

set -euo pipefail

NATS_VERSION="2.10.24"
INSTALL_DIR="$(dirname "$0")"
DATA_DIR="${INSTALL_DIR}/data"
CONF_FILE="${INSTALL_DIR}/nats-server.conf"

# Detect OS/arch
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
esac

BINARY="${INSTALL_DIR}/nats-server"

# Download if not present
if [ ! -f "$BINARY" ]; then
    echo "📦 Downloading NATS server v${NATS_VERSION} for ${OS}/${ARCH}..."
    DOWNLOAD_URL="https://github.com/nats-io/nats-server/releases/download/v${NATS_VERSION}/nats-server-v${NATS_VERSION}-${OS}-${ARCH}.tar.gz"
    curl -fsSL "$DOWNLOAD_URL" | tar -xz --strip-components=1 -C "$INSTALL_DIR" "nats-server-v${NATS_VERSION}-${OS}-${ARCH}/nats-server"
    chmod +x "$BINARY"
    echo "✅ NATS server installed at ${BINARY}"
fi

# Create data directory
mkdir -p "$DATA_DIR/jetstream"

echo "🚀 Starting local NATS server (JetStream enabled)..."
echo "   Config: ${CONF_FILE}"
echo "   Data:   ${DATA_DIR}"
echo "   Port:   4222"
echo "   Monitor: http://localhost:8222"
echo ""

exec "$BINARY" --config "$CONF_FILE"
