# Railway NATS auth + ws_bridge deployment

## Why this exists

Railway NATS was previously reachable without auth, and non-Railway hosts silently fell back to the Railway-internal hostname (`nats://nats.railway.internal:4222`). That works only inside Railway and creates misleading connection failures on the Mac mini / remote boxes.

This repo now hard-fails when `NATS_RAILWAY_URL` is missing outside Railway, and it supports a dedicated Railway auth token via `NATS_RAILWAY_AUTH_TOKEN`.

## Railway NATS service

Config lives in `infra/railway-nats/nats-server.conf`.

Required Railway env vars for the NATS service:

- `NATS_AUTH_TOKEN=<strong random secret>`

Recommended public/private URLs:

- Private internal URL (inside Railway): `nats://nats.railway.internal:4222`
- Public TCP URL (outside Railway): `nats://<public-host>:<public-port>`
  - Current known production endpoint: `nats://maglev.proxy.rlwy.net:55041`

### Connectors that must receive Railway credentials

Any process that talks to Railway NATS from outside the Railway private network should receive:

- `NATS_RAILWAY_URL`
- `NATS_RAILWAY_AUTH_TOKEN`

Examples:

- Mac mini / local Docker compose bridge
- `memu.boot` gateway containers
- `memu.ws_bridge`
- `memu.event_consumer`
- `memu.session_bus`

## ws_bridge Railway service

A dedicated Railway deployment spec now lives at `infra/railway-ws-bridge/railway.json`, backed by `Dockerfile.ws_bridge`.

Recommended env vars for the `ws_bridge` Railway service:

- `NATS_LOCAL_URL=nats://localhost:4222` only if the service also has a colocated local NATS sidecar
- `NATS_RAILWAY_URL=nats://nats.railway.internal:4222`
- `NATS_RAILWAY_AUTH_TOKEN=${NATS_AUTH_TOKEN}`
- `PORT=8001` if Railway injects it for HTTP services

If `ws_bridge` is running outside Railway instead, set:

- `NATS_LOCAL_URL=nats://<local-nats-host>:4222`
- `NATS_RAILWAY_URL=nats://maglev.proxy.rlwy.net:55041`
- `NATS_RAILWAY_AUTH_TOKEN=<same strong token used by Railway NATS>`

## Manual rollout checklist

1. Generate a token: `python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY`
2. Set `NATS_AUTH_TOKEN` on the Railway NATS service.
3. Redeploy Railway NATS so auth is enforced.
4. Set `NATS_RAILWAY_URL` and `NATS_RAILWAY_AUTH_TOKEN` on every external connector.
5. Deploy the `ws_bridge` service with `infra/railway-ws-bridge/railway.json`.
6. Verify:
   - `nc -z maglev.proxy.rlwy.net 55041`
   - `curl -fsS https://api-production-86f5.up.railway.app/api/v1/memu/health`
   - `curl -fsS http://localhost:8222/healthz`
   - `curl -fsS http://localhost:8001/api/cluster/status`

## JetStream / bridge expectations

`memu.ws_bridge` subscribes to `swarm.>` on the currently active NATS connection exposed by `NATSClusterManager`. The bridge is healthy when:

- local NATS is active during local operation
- Railway NATS becomes active when local NATS is unavailable
- `/api/cluster/status` shows both node URLs and the currently active node

This bridge is a visibility/control plane component, not a subject-rewriting replicator. It should be deployed anywhere the Glass Box UI needs direct mesh visibility.
