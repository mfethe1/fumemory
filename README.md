# memU

**Free, open-source shared memory for AI agents.**

Run your own memory layer on Postgres + pgvector. No monthly SaaS lock-in.

## Why memU

- Own your data (self-hosted)
- Fast semantic search for agent memory
- Agent-aware namespacing and filtering
- Temporal scoring (recency + frequency)
- Memory decay and reinforcement
- Duplicate collapse and memory chaining
- No `/store` endpoint — API is stable on `/memories`, `/search`, `/chat`

## Core APIs

- `GET /health` — health check
- `POST /memories` — add a memory
- `POST /search` — semantic query
- `POST /chat` — RAG-over-memory chat
- `GET /memories/{id}` — fetch one memory
- `DELETE /memories/{id}` — remove one memory
- `POST /memories/bulk` — bulk import

### Auth

Every request uses `X-API-Key`.

## Quick Start

### Run with Docker (recommended)

```bash
git clone https://github.com/mfethe1/fumemory.git
cd fumemory
docker compose up -d
```

API: `http://localhost:8000`  
Bridge: `http://localhost:8001`

### Local install

```bash
git clone https://github.com/mfethe1/fumemory.git
cd fumemory
pip install -e .

docker compose up -d postgres nats
uvicorn memu.api:app --host 0.0.0.0 --port 8000
```

## API Examples

```bash
curl -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "content": "Always rotate API keys before release",
    "memory_type": "lesson",
    "agent_id": "lenny",
    "metadata": {"project": "operations"}
  }'

curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "query": "production incidents",
    "limit": 10,
    "agent_id": null,
    "memory_type": "lesson"
  }'

curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "question": "What did we learn from last outage?",
    "agent_id": null
  }'
```

## Swarm OS + coordination

memU now includes an agent coordination layer for production-like multi-agent runs:

- `lane_lock.py`: lane ownership and contention control
- `warden_runtime.py`: heartbeat watchdog + recovery behavior
- `bridge_ledger.py`: append-only event log + DAG views
- `ws_bridge.py`: WebSocket ↔ NATS bridge for cross-network relays
- `cluster.py` / `boot.py`: startup and cluster registration
- `hardening.py`: failure-mode mitigations

NATS subjects used today:
- `memu.heartbeat`
- `memu.lane.*`
- `memu.events`
- `memu.bridge.*`

## Project layout

```text
memu/
  api.py              # FastAPI service
  client.py           # Client helper
  models.py           # Pydantic schemas
  decay.py            # Decay/strengthen + dedupe
  boot.py             # Bootstrap sequence
  cluster.py          # Cluster metadata
  lane_lock.py        # Exclusive task ownership
  warden.py           # Warden core
  warden_runtime.py   # Runtime loop
  bridge_ledger.py    # Event ledger + trajectory
  ws_bridge.py        # WebSocket↔NATS
  hardening.py        # Reliability mitigations
  projection.py
  swarm_models.py

docker-compose.yml
Dockerfile
pyproject.toml
```

## Environment

- `DATABASE_URL` (required)
- `MEMU_API_KEY` (required for `X-API-Key`)
- `OPENAI_API_KEY` (optional embedding source)
- `EMBEDDING_BASE_URL` (optional)
- `EMBEDDING_MODEL` (optional)
- `EMBEDDING_DIMS` (optional)
- `NATS_LOCAL_URL`, `NATS_RAILWAY_URL`

## Deployment (Railway)

Use the included `Dockerfile` and set at least:

- `DATABASE_URL`
- `MEMU_API_KEY`
- `OPENAI_API_KEY` (if embeddings use it)

## License

MIT — see `LICENSE`
