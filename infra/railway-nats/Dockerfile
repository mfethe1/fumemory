# NATS JetStream — Railway fallback node
# Uses official nats image — no build needed

FROM nats:2.10-alpine
RUN mkdir -p /data/jetstream
VOLUME ["/data/jetstream"]

# Copy config (path relative to repo root since Railway builds from root)
COPY infra/railway-nats/nats-server.conf /etc/nats/nats-server.conf

EXPOSE 4222 8222

CMD ["nats-server", "--config", "/etc/nats/nats-server.conf"]
