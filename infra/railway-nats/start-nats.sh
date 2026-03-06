#!/bin/sh
cat <<CONF > /etc/nats/nats-server.conf
listen: 0.0.0.0:${RAILWAY_TCP_APPLICATION_PORT:-4222}
http_port: 8222
jetstream {
  store_dir: /data/jetstream
  max_mem: 256MB
  max_file: 1GB
}
CONF
echo "Starting NATS with config:"
cat /etc/nats/nats-server.conf
exec nats-server -c /etc/nats/nats-server.conf
