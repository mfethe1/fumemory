---
name: fumemory-swarm
description: Connect to and operate the fumemory distributed swarm OS. Use when publishing events to NATS, querying the bridge DAG/events/trajectory APIs, checking swarm health, troubleshooting NATS connectivity, working with the Glass Box UI, building gateway containers, or coordinating cross-agent swarm work. Covers NATS pub/sub, dual-cluster failover, bridge REST endpoints, Warden container control, and agent lane locking.
---

# fumemory Swarm OS Skill

## Architecture Overview

```
NATS JetStream (dual-cluster)
├─ Primary: Lenny's Linux (100.76.63.58:4222 via Tailscale)
├─ Fallback: Railway (gondola.proxy.rlwy.net:22393)
└─ Auto-failover: <100ms

WebSocket Bridge (port 8001)
├─ Subscribes: swarm.> (all subjects)
├─ In-memory DAG ledger
├─ REST API + WebSocket streaming
└─ God Mode (halt/amend)

Glass Box UI (port 3001)
├─ React Flow DAG visualization
├─ Real-time WebSocket consumer
└─ Compute budget meter

memU API (Railway)
└─ https://api-production-86f5.up.railway.app
```

## Quick Start

### Check swarm health
```bash
python scripts/swarm_health.py
```

### Publish a test event
```python
import asyncio, nats, json, uuid
from datetime import datetime, timezone

async def publish():
    nc = await nats.connect("nats://100.76.63.58:4222")
    event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_gateway": "YOUR_AGENT_NAME",
        "event_type": "task_drafted",
        "task_id": str(uuid.uuid4()),
        "payload": {"title": "My Task", "root_prompt_id": str(uuid.uuid4()), "parent_task_id": None, "compute_budget": 0.50},
        "compute_cost": 0.001,
    }
    await nc.publish("swarm.events.live", json.dumps(event).encode())
    await nc.drain()

asyncio.run(publish())
```

### Query bridge REST API
```bash
# DAG (all tasks + edges)
curl http://127.0.0.1:8001/api/dag

# Events (paginated)
curl http://127.0.0.1:8001/api/events?limit=50

# Single task trajectory
curl http://127.0.0.1:8001/api/trajectory/{task_id}

# Aggregate stats
curl http://127.0.0.1:8001/api/stats

# Cluster health
curl http://127.0.0.1:8001/api/cluster/status

# God Mode: halt
curl -X POST http://127.0.0.1:8001/api/halt -H "Content-Type: application/json" -d '{"root_prompt_id":"...","reason":"..."}'

# God Mode: amend task
curl -X POST http://127.0.0.1:8001/api/amend/{task_id} -H "Content-Type: application/json" -d '{"correction":"..."}'
```

## NATS Connection

### URLs (from .env or environment)
- `NATS_LOCAL_URL=nats://100.76.63.58:4222` (Lenny, Tailscale)
- `NATS_RAILWAY_URL=nats://gondola.proxy.rlwy.net:22393` (Railway fallback)

### Subject Hierarchy
| Subject | Purpose |
|---------|---------|
| `swarm.events.live` | Production task events |
| `swarm.events.test` | Test events |
| `swarm.heartbeat` | Gateway heartbeats |
| `swarm.dlq` | Dead Letter Queue |
| `swarm.rpc.request` | Mesh RPC requests |
| `swarm.rpc.response.{gateway_id}` | RPC responses |
| `swarm.checkpoint.{task_id}` | Task checkpoints |
| `swarm.advisory.suicide` | Ghost gateway kill signal |
| `swarm.warden.*` | Container lifecycle events |

### Event Types
`task_drafted`, `bid_submitted`, `lease_granted`, `task_claimed`, `task_completed`, `task_failed`, `task_cancelled`, `task_amended`, `audit_proposed`, `audit_accepted`, `audit_rejected`, `lease_expired`, `circuit_breaker`, `system_halt`, `system_override`, `heartbeat_ping`, `lane_contested`, `lane_acquired`, `dlq_entry`

### SwarmEvent Envelope (required for all events)
```json
{
  "event_id": "uuid",
  "timestamp": "ISO-8601",
  "source_gateway": "agent_name",
  "event_type": "task_drafted",
  "task_id": "uuid",
  "payload": {},
  "compute_cost": 0.001
}
```

## Troubleshooting

### NATS not reachable
1. Check Tailscale: `tailscale status` — is Lenny's node online?
2. TCP test: `python -c "import socket; s=socket.create_connection(('100.76.63.58', 4222), 5); print('OK'); s.close()"`
3. If local down, verify Railway fallback: `python -c "import asyncio,nats; asyncio.run(nats.connect('nats://gondola.proxy.rlwy.net:22393'))"`

### Bridge not processing events
- The bridge must be running BEFORE events are published (NATS pub/sub is real-time, not replayed)
- Check: `curl http://127.0.0.1:8001/api/stats` — if `total_events: 0` after publishing, bridge wasn't subscribed

### Restarting services
```powershell
# Bridge (Windows)
cd C:\Users\mfeth\.openclaw\workspace\fumemory
$env:NATS_LOCAL_URL="nats://100.76.63.58:4222"
$env:NATS_RAILWAY_URL="nats://gondola.proxy.rlwy.net:22393"
python -c "import uvicorn; uvicorn.run('memu.ws_bridge:app', host='0.0.0.0', port=8001)"

# Glass Box UI (Windows)
cd C:\Users\mfeth\.openclaw\workspace\fumemory\ui
npx vite --port 3001
```

```bash
# Bridge (Linux — Lenny)
cd /home/michael-fethe/.openclaw/workspace/memu-oss
export NATS_LOCAL_URL=nats://localhost:4222
export NATS_RAILWAY_URL=nats://gondola.proxy.rlwy.net:22393
python -c "import uvicorn; uvicorn.run('memu.ws_bridge:app', host='0.0.0.0', port=8001)"
```

## Repo & Files
- **Repo**: `https://github.com/mfethe1/fumemory.git` (branch: `main`)
- **Local (Windows)**: `C:\Users\mfeth\.openclaw\workspace\fumemory`
- **Local (Linux)**: `/home/michael-fethe/.openclaw/workspace/memu-oss`
- **Key files**: `memu/ws_bridge.py`, `memu/cluster.py`, `memu/bridge_ledger.py`, `memu/swarm_models.py`, `memu/lane_lock.py`, `memu/warden.py`, `memu/boot.py`, `memu/hardening.py`, `memu/projection.py`

## Cross-Agent Coordination

All agents share the same NATS mesh. To coordinate:

1. **Publish events** to `swarm.events.live` with your `source_gateway` set to your agent name
2. **Subscribe** to `swarm.>` to see all swarm activity
3. **Query bridge** at `http://127.0.0.1:8001/api/` for current state
4. **Lane locking**: Use NATS KV bucket `RESOURCE_LANES` — only one agent per lane at a time
5. **Health script**: Run `scripts/swarm_health.py` to check full stack status

For detailed schemas, see `references/event_schemas.md`.
For Warden container operations, see `references/warden_ops.md`.
