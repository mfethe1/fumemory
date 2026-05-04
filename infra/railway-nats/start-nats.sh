#!/bin/sh
set -e
NATS_PORT="${RAILWAY_TCP_APPLICATION_PORT:-4222}"

# Unset PORT so NATS doesn't try to use it for the client port.
unset PORT

echo "Starting NATS with port=$NATS_PORT, monitor=8222, jetstream=/data/jetstream"
exec nats-server -p "$NATS_PORT" -m 8222 -js -sd /data/jetstream
