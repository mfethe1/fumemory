<![CDATA[<div align="center">

# 🧠 memU

### Free, open-source shared memory for AI agents.

**Stop paying for memory. Your agents deserve better.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![PostgreSQL 16+](https://img.shields.io/badge/postgres-16+-336791.svg)](https://postgresql.org)

[Quick Start](#quick-start) · [Features](#features) · [Architecture](#architecture) · [API Reference](#api-reference) · [Swarm OS](#swarm-os) · [Contributing](CONTRIBUTING.md)

</div>

---

## Why pay for memory?

SuperMemory charges you monthly. Mem0 wants your data on their servers. Zep locks you into their cloud.

**memU is free. Forever. Run it on your own Postgres.**

---

## Features

- 🔍 **Semantic vector search** — pgvector-powered similarity search across all memories
- 🤖 **Multi-agent namespacing** — agents share one pool, search across all or filter by agent
- ⏰ **Temporal awareness** — recency-weighted scoring, not just cosine similarity
- 📉 **Memory decay & reinforcement** — accessed memories get boosted, stale ones fade
- 🏷️ **Typed memories** — fact, decision, lesson, pattern, failure
- 🔗 **Memory chains** — link related memories (decision → outcome → lesson)
- 🔄 **Deduplication** — near-duplicate detection on upsert, merge instead of duplicate
- 📥 **Bulk import** — ingest markdown, JSON, or existing knowledge bases
- 💬 **RAG chat** — conversational queries over your entire knowledge base
- 🐝 **Swarm OS** — NATS-based agent coordination with lane-lock, warden watchdog, and event ledger
- 🐳 **One command to run** — `docker compose up` and you're done

---

## Comparison

| Feature | memU | SuperMemory | Mem0 | Zep |
|---------|------|-------------|------|-----|
| **Price** | **Free forever** | $29+/mo | Usage-based | $99+/mo |
| **Self-hosted** | ✅ | ❌ | Partial | Partial |
| **Multi-agent** | ✅ Native | ❌ | ❌ | ❌ |
| **Memory decay** | ✅ | ❌ | ❌ | ❌ |
| **Memory chains** | ✅ | ❌ | ❌ | ❌ |
| **Agent coordination** | ✅ Swarm OS | ❌ | ❌ | ❌ |
| **Open source** | ✅ MIT | ❌ | Partial | ❌ |

---

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/mfethe1/fumemory.git
cd fumemory
docker compose up -d
```

memU is running at `http://localhost:8000`. The NATS bridge runs on `:8001`.

### From source

```bash
git clone https://github.com/mfethe1/fumemory.git
cd fumemory
pip install -e .

# Start Postgres + NATS
docker compose up -d postgres nats

# Run the API
uvicorn memu.api:app --host 0.0.0.0 --port 8000
```

### pip install

```bash
pip install memu-memory
```

```python
from memu import MemUClient

client = MemUClient("http://localhost:8000", api_key="your-key")

# Store a memory
client.add("The deployment failed because we forgot to run migrations",
           memory_type="lesson",
           agent_id="lenny")

# Search across all agents
results = client.search("deployment failures", limit=5)

# Chat with your memory base
answer = client.chat("What have we learned about deployments?")
```

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   Your Agents                    │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐       │
│  │Agent1│  │Agent2│  │Agent3│  │Agent4│  ...   │
│  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘       │
│     └─────────┴────┬────┴─────────┘             │
│              ┌─────▼──────┐                      │
│              │ memU Client │  (Python / REST)    │
│              └─────┬──────┘                      │
└────────────────────┼────────────────────────────┘
                     │
         ┌───────────┼───────────┐
         │           │           │
  ┌──────▼──────┐  ┌─▼──┐  ┌────▼─────┐
  │  memU API   │  │NATS│  │ WS Bridge│
  │  :8000      │  │    │  │  :8001   │
  │ /memories   │  │    │  │          │
  │ /search     │  │    │  │          │
  │ /chat       │  │    │  │          │
  └──────┬──────┘  └─┬──┘  └────┬─────┘
         │           │           │
         └───────────┼───────────┘
              ┌──────▼──────┐
              │  PostgreSQL  │
              │  + pgvector  │
              └──────────────┘
```

### Services

| Service | Port | Purpose |
|---------|------|---------|
| **api** | 8000 | FastAPI — memory CRUD, search, chat, health |
| **bridge** | 8001 | WebSocket ↔ NATS bridge for real-time agent coordination |
| **postgres** | 5432 | pgvector storage — memories, embeddings, event ledger |
| **nats** | 4222 | Pub/sub messaging for Swarm OS coordination |

---

## API Reference

### `POST /memories` — Store a memory

```bash
curl -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "content": "Always run migrations before deploying",
    "memory_type": "lesson",
    "agent_id": "lenny",
    "metadata": {"project": "infrastructure"}
  }'
```

### `POST /search` — Semantic search

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "query": "deployment mistakes",
    "limit": 10,
    "agent_id": null,
    "memory_type": "lesson",
    "temporal_weight": 0.3
  }'
```

### `POST /chat` — RAG chat over memories

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-key" \
  -d '{
    "question": "What do we know about CI/CD best practices?",
    "agent_id": null
  }'
```

### `GET /health` — Health check

### `GET /memories/{id}` — Get a specific memory

### `DELETE /memories/{id}` — Delete a memory

### `POST /memories/bulk` — Bulk import from markdown/JSON

---

## Memory Types

| Type | Use Case |
|------|----------|
| `fact` | Concrete information: "The API runs on port 8000" |
| `decision` | Choices made: "We chose Postgres over Redis for persistence" |
| `lesson` | Things learned: "Always check migrations before deploy" |
| `pattern` | Recurring observations: "Users churn when onboarding takes >5 min" |
| `failure` | What went wrong: "Forgot to rotate API keys, caused 2h outage" |

---

## Memory Decay & Reinforcement

```
score = similarity × (1 - decay_rate)^days × (1 + log(access_count + 1)) × temporal_weight
```

- Accessed memories get reinforced on every search hit
- Old unused memories decay (configurable rate, default 0.01/day)
- Temporal weight blends pure similarity with recency

---

## Memory Chains

Link related memories to build knowledge graphs:

```python
decision = client.add("Switched from REST to GraphQL for mobile API",
                       memory_type="decision")

outcome = client.add("GraphQL reduced mobile API calls by 60%",
                      memory_type="fact",
                      parent_id=decision.id)

lesson = client.add("GraphQL works well for mobile clients with variable data needs",
                     memory_type="lesson",
                     parent_id=decision.id)
```

---

## Swarm OS

memU includes a coordination layer for multi-agent teams, built on NATS messaging.

### Components

| Module | Purpose |
|--------|---------|
| `lane_lock.py` | Exclusive task ownership — one agent per lane, no conflicts |
| `warden.py` / `warden_runtime.py` | Heartbeat watchdog — detects stale agents and triggers respawn |
| `bridge_ledger.py` | Append-only event ledger with DAG, trajectory, and stats endpoints |
| `ws_bridge.py` | WebSocket ↔ NATS bridge for cross-network agent communication |
| `cluster.py` | Cluster membership and peer discovery |
| `boot.py` | Bootstrap sequence — connects NATS, registers agent, starts warden |
| `hardening.py` | 10 failure-mode mitigations (network partition, stale locks, etc.) |

### NATS Subjects

- `memu.heartbeat` — agent liveness pings
- `memu.lane.*` — lane claim/release events
- `memu.events` — general coordination events
- `memu.bridge.*` — cross-bridge message relay

### How it works

1. Agents boot via `boot.py`, connect to NATS, register with the cluster
2. The warden watches heartbeats — if an agent goes silent, it triggers respawn
3. Lane-lock ensures exclusive task ownership (no two agents work the same task)
4. The bridge ledger records all events as an append-only log with DAG structure
5. The WS bridge allows agents on different networks to communicate via WebSocket relay

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql://memu:memu@localhost:5432/memu` | Postgres connection |
| `MEMU_API_KEY` | `memu-dev-key` | API key for auth (X-API-Key header) |
| `OPENAI_API_KEY` | — | For embeddings (if using OpenAI) |
| `EMBEDDING_BASE_URL` | `http://localhost:11434` | Embedding endpoint |
| `EMBEDDING_MODEL` | `qwen3-embedding` | Model name |
| `EMBEDDING_DIMS` | `4096` | Vector dimensions |
| `DEDUP_THRESHOLD` | `0.95` | Similarity threshold for dedup |
| `DECAY_RATE` | `0.01` | Daily decay rate |
| `NATS_LOCAL_URL` | `nats://nats:4222` | Local NATS server |
| `NATS_RAILWAY_URL` | — | Remote NATS for cross-network bridge |

---

## Deployment

### Railway

memU deploys to Railway with the included `Dockerfile`. Set env vars in your Railway project:

- `DATABASE_URL` (Railway Postgres addon or external)
- `MEMU_API_KEY` (any string — this is your auth key)
- `OPENAI_API_KEY` (for embeddings)

### Self-hosted

```bash
docker compose up -d
```

Production: set `MEMU_API_KEY` to a strong random value and configure a real Postgres instance.

---

## Project Structure

```
memu-oss/
├── memu/
│   ├── api.py              # FastAPI app — all REST endpoints
│   ├── models.py           # Pydantic models
│   ├── client.py           # Python client SDK
│   ├── decay.py            # Decay/reinforcement/dedup logic
│   ├── boot.py             # Agent bootstrap sequence
│   ├── cluster.py          # Cluster membership
│   ├── lane_lock.py        # Exclusive task ownership
│   ├── warden.py           # Heartbeat watchdog
│   ├── warden_runtime.py   # Warden runtime loop
│   ├── bridge_ledger.py    # Append-only event DAG
│   ├── ws_bridge.py        # WebSocket ↔ NATS bridge
│   ├── hardening.py        # Failure-mode mitigations
│   ├── projection.py       # Event projections
│   └── swarm_models.py     # Swarm data models
├── infra/
│   └── local-nats/         # NATS server config
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── CONTRIBUTING.md
├── SECURITY.md
└── README.md
```

---

## License

[MIT](LICENSE)

---

<p align="center">Built by <a href="https://protelynx.ai">Protelynx</a></p>
]]>