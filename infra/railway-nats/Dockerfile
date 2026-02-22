# NATS JetStream — Railway deployment
#
# Lightweight, high-performance message broker for the fumemory swarm mesh.
# This is the FALLBACK instance. Local NATS is primary.
#
# Deploy on Railway:
#   1. New service → Docker image
#   2. Point to this Dockerfile (or just use image: nats:latest)
#   3. Set port: 4222
#   4. Add volume mount: /data
#   5. Optionally set NATS_AUTH_TOKEN env var for auth

FROM nats:2.10-alpine

# JetStream config with persistence
COPY nats-server.conf /etc/nats/nats-server.conf

EXPOSE 4222 8222

CMD ["nats-server", "--config", "/etc/nats/nats-server.conf"]
