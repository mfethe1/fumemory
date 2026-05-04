# Warden Container Operations Reference

## Overview

Warden is the **non-LLM, deterministic** container control plane. It is the ONLY module
allowed to import Docker SDK. No LLM reasoning in the Warden loop — pure Python logic.

## Container Lifecycle

```
REQUESTED → SPAWNING → RUNNING → (HEALTHY | UNHEALTHY) → STOPPED
                                       ↓
                                   RESPAWNING
```

## Spawning a Gateway Container

```python
import docker
import os

client = docker.from_env()

container = client.containers.run(
    image="fumemory-gateway:latest",
    name=f"gw-{gateway_id}",
    detach=True,
    environment={
        "GATEWAY_ID": gateway_id,
        "NATS_LOCAL_URL": "nats://host.docker.internal:4222",
        "NATS_RAILWAY_URL": os.environ["NATS_RAILWAY_URL"],
        "MEMU_API_URL": os.environ["MEMU_API_URL"],
        "MEMU_API_KEY": "${MEMU_API_KEY}",  # Set via environment variable, never hardcode
        "MAX_HYDRATION_TOKENS": "8000",
        "WARDEN_WARM_POOL_MODE": "cold",
    },
    mem_limit="512m",
    cpu_period=100000,
    cpu_quota=50000,  # 0.5 CPU
    restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
    network_mode="bridge",
)
```

## Heartbeat Watchdog

The Warden monitors all gateway containers via NATS heartbeats:

1. Each gateway publishes `heartbeat_ping` every 3 seconds to `swarm.heartbeat`
2. Warden subscribes to `swarm.heartbeat` and tracks last-seen timestamps
3. If a gateway misses 3 consecutive heartbeats (>10s), Warden:
   - Publishes `lease_expired` for any tasks the gateway held
   - Kills the container
   - Respawns if the task is still pending

## Respawn Logic

```python
async def handle_missed_heartbeat(gateway_id: str):
    # 1. Kill container
    container = client.containers.get(f"gw-{gateway_id}")
    container.kill()
    
    # 2. Publish lease_expired events for all tasks this gateway held
    for task_id in get_tasks_for_gateway(gateway_id):
        await publish_event("lease_expired", task_id, {"gateway_id": gateway_id})
    
    # 3. Respawn (cold start)
    spawn_gateway(new_gateway_id)
```

## HMAC Signing

All Warden→Gateway commands are HMAC-signed to prevent spoofing:

```python
import hmac, hashlib

def sign_command(command: dict, secret: str) -> str:
    payload = json.dumps(command, sort_keys=True)
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
```

## Advisory Suicide Signal

If Warden detects a ghost gateway (split-brain), it publishes to `swarm.advisory.suicide`:

```json
{"target_gateway": "ghost-gw-id", "reason": "fencing_token_stale"}
```

The ghost gateway's boot module listens for this and calls `sys.exit(0)` before any DB write.

## Docker on Linux (Lenny's box)

```bash
# Build gateway image
cd /home/michael-fethe/.openclaw/workspace/memu-oss
docker build -t fumemory-gateway:latest -f Dockerfile.gateway .

# Run a gateway
docker run -d --name gw-lenny \
  -e GATEWAY_ID=lenny \
  -e NATS_LOCAL_URL=nats://host.docker.internal:4222 \
  -e NATS_RAILWAY_URL="$NATS_RAILWAY_URL" \
  -e MEMU_API_URL="$MEMU_API_URL" \
  --memory=512m --cpus=0.5 \
  fumemory-gateway:latest
```

## Key Constraints
- `WARDEN_WARM_POOL_MODE=cold` (7.6GB RAM on Windows, conserve memory)
- No host volume mounts to gateway containers (security)
- Warden is the SOLE Docker SDK user — no other module touches containers
- `WARDEN_SECRET` must be set (empty = fail-closed, all commands rejected)
