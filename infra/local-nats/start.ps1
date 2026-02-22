# Setup script for local NATS instance (primary node) — Windows
# Downloads NATS server binary and starts with JetStream enabled

$ErrorActionPreference = "Stop"

$NATS_VERSION = "2.10.24"
$INSTALL_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$DATA_DIR = Join-Path $INSTALL_DIR "data"
$CONF_FILE = Join-Path $INSTALL_DIR "nats-server.conf"
$BINARY = Join-Path $INSTALL_DIR "nats-server.exe"

# Download if not present
if (-not (Test-Path $BINARY)) {
    Write-Host "📦 Downloading NATS server v$NATS_VERSION for windows/amd64..."
    $url = "https://github.com/nats-io/nats-server/releases/download/v${NATS_VERSION}/nats-server-v${NATS_VERSION}-windows-amd64.zip"
    $zipPath = Join-Path $INSTALL_DIR "nats-server.zip"

    Invoke-WebRequest -Uri $url -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $INSTALL_DIR -Force

    # Move binary from extracted folder
    $extracted = Join-Path $INSTALL_DIR "nats-server-v${NATS_VERSION}-windows-amd64"
    Move-Item (Join-Path $extracted "nats-server.exe") $BINARY -Force
    Remove-Item $extracted -Recurse -Force
    Remove-Item $zipPath -Force

    Write-Host "✅ NATS server installed at $BINARY"
}

# Create data directory
New-Item -ItemType Directory -Force -Path (Join-Path $DATA_DIR "jetstream") | Out-Null

Write-Host ""
Write-Host "🚀 Starting local NATS server (JetStream enabled)..."
Write-Host "   Config: $CONF_FILE"
Write-Host "   Data:   $DATA_DIR"
Write-Host "   Port:   4222"
Write-Host "   Monitor: http://localhost:8222"
Write-Host ""

& $BINARY --config $CONF_FILE
