#!/bin/sh
set -e
NATS_PORT="${RAILWAY_TCP_APPLICATION_PORT:-4222}"
CONF=/tmp/nats-server.conf
cat > "$CONF" <<CONF
listen: 0.0.0.0:${NATS_PORT}
http_port: 8222
jetstream {
  store_dir: /data/jetstream
  max_mem: 256MB
  max_file: 1GB
}
CONF
echo "=== NATS config ==="
cat "$CONF"
echo "==================="
unset PORT
exec nats-server -c "$CONF"
