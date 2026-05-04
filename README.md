<div align="center">

# fumemory (memU)

### Free, open-source Memory Evidence Plane for OpenClaw agents.

**Durable evidence writes. Learning recall. Federation proof. Your Postgres.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![PostgreSQL 16+](https://img.shields.io/badge/postgres-16+-336791.svg)](https://postgresql.org)

[Quick Start](#quick-start) · [Features](#features) · [Architecture](#architecture) · [API Reference](#api-reference) · [Contributing](CONTRIBUTING.md)

</div>

---

## What is fumemory?

fumemory is the **Memory Evidence Plane** for OpenClaw-driven agent work. It records durable Evidence Memory, distills Learning Memory, proves Federation Readiness, and delivers Forensic Recall — while **OpenClaw remains the top-level coordinator** for task routing and gateway selection.

SuperMemory charges you monthly. Mem0 wants your data on their servers. Zep locks you into their cloud.

**fumemory is free. Forever. Run it on your own Postgres.**

---

## Features

- **Evidence Memory** — immutable, task-bound execution proof written synchronously by OpenClaw gateways
- **Learning Memory** — distilled reusable insights derived from evidence after reflection and review
- **Canonical Write** — synchronous `/api/v1/memu/add` path; evidence is searchable before OpenClaw proceeds
- **Forensic Recall** — explicit recall mode returning Evidence Memory with task/session/gateway provenance for proof and audit
- **Default Learning Recall** — concise agent context from accepted Learning Memory only; raw evidence never leaks
- **Reflection Review** — six-hour Telegram review window for proposed Learning Memory before automatic integration
- **Compact Telegram Notices** — proposed learning delivered as summary + actions; no raw evidence in chat
- **Idempotency** — canonical evidence writes deduplicate by `(tenant_id, idempotency_key)`
- **Versioned Embedding Contract** — `EMBEDDING_API_BASE` / `EMBEDDING_MODEL` / `EMBEDDING_DIMS`; dimension changes are additive
- **Core API Readiness** — API + Postgres + auth + canonical write + recall; no NATS or Temporal required
- **Federation Readiness** — Core API + Railway NATS/JetStream + searchable gateway proof
- **Semantic vector search** — pgvector-powered similarity search over all memories
- **Multi-agent namespacing** — agents share one memory pool, search across all or filter by agent
- **Temporal awareness** — recency-weighted search, not just cosine similarity
- **Memory decay & reinforcement** — frequently accessed memories get boosted, stale ones fade
- **Memory chains** — link related memories (decision → outcome → lesson learned)
- **NATS mesh** — multi-machine clustering with local + Railway failover
- **Warden** — container orchestration with heartbeat watchdog and Dead Man's Switch
- **Glass Box UI** — real-time WebSocket dashboard for swarm observability
- **One command to run** — `docker compose up` and you're done

---

## Comparison

| Feature | memU | SuperMemory | Mem0 | Zep |
|---------|------|-------------|------|-----|
| **Price** | **Free forever** | $29+/mo | Usage-based | $99+/mo |
| **Self-hosted** | Yes | No | Partial | Partial |
| **Multi-agent** | Yes, native | No | No | No |
| **Memory decay** | Yes | No | No | No |
| **Memory types** | 5 types | No | No | Basic |
| **Memory chains** | Yes | No | No | No |
| **Deduplication** | Yes | No | Basic | No |
| **Temporal weighting** | Yes | No | No | Yes |
| **Multi-machine clustering** | Yes (NATS) | No | No | No |
| **Container orchestration** | Yes (Warden) | No | No | No |
| **Your data stays yours** | Yes | No | No | No |
| **Open source** | MIT | No | Partial | No |

---

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/mfethe1/fumemory.git
cd fumemory
docker compose up -d
```

memU is running at `http://localhost:8000`.

### Python client

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

---

## Architecture

fumemory is the **Memory Evidence Plane**. OpenClaw remains the top-level coordinator; fumemory stores proof and surfaces learning.

```text
OpenClaw Coordinator
        |
        | assigns work, owns routing, owns task state
        v
OpenClaw Gateway / Agent Runtime
        |
        | canonical sync evidence write (POST /api/v1/memu/add)
        v
+------------------------------+
| fumemory Memory Evidence API |
| - schema validation          |
| - idempotency                |
| - auth and tenant scope      |
| - immediate searchability    |
+---------------+--------------+
                |
                v
+------------------------------+
| Memory Store                 |
| - Evidence Memory (immutable)|
| - Learning Memory (derived)  |
| - source evidence links      |
| - temporal validity          |
| - vector, lexical, graph idx |
+-----+------------------+-----+
      |                  |
      |                  | background reflection
      |                  v
      |          Reflection Worker
      |          - clusters evidence
      |          - distills learning
      |          - sends Compact Telegram Notice
      |          - 6h review window before integration
      |
      v
Recall Service
- default learning recall (accepted Learning Memory only)
- explicit forensic recall (Evidence Memory + provenance)
      |
      v
OpenClaw prompt/context injection

Railway services:
  api (required) + postgres-pgvector (required)
  + nats-jetstream (federation) + temporal-worker (async) + embedding-service (optional)
```

### Readiness gates

| Gate | Requires | How to verify |
|------|----------|---------------|
| **Core API Readiness** | `api` + `postgres-pgvector` | `python scripts/verify_deployment.py --api-url <url>` |
| **Federation Readiness** | Core API + `nats-jetstream` + searchable proof | `python scripts/verify_deployment.py --api-url <url> --check-federation` |
| **Async Readiness** | Core API + `temporal-worker` | `python scripts/verify_deployment.py --api-url <url> --check-async` |

Core API Readiness does **not** require NATS or Temporal. Federation and async gates are additive.

### Components

| Component | What it does |
|-----------|-------------|
| **fumemory API** (`memu/api.py`) | FastAPI server — canonical write, learning recall, forensic recall, reflection queue |
| **PostgreSQL + pgvector** | Storage — Evidence Memory, Learning Memory, embeddings, chains, decay scores |
| **NATS Cluster** (`memu/cluster.py`) | Multi-machine mesh — local primary + Railway fallback; required for Federation Readiness |
| **Reflection Worker** (`memu/reflection.py`) | Background worker — distills evidence into Learning Memory proposals, Telegram notices |
| **Warden** (`memu/warden.py`) | Container lifecycle — heartbeat watchdog, respawn, warm pool, Dead Man's Switch |
| **WS Bridge** (`memu/ws_bridge.py`) | WebSocket event stream — real-time swarm observability |
| **Glass Box UI** (`ui/`) | React dashboard — live view of agent activity, DAG visualization |
| **Bridge Ledger** (`memu/bridge_ledger.py`) | In-memory event log — DAG, trajectories, stats REST endpoints |
| **Lane Lock** (`memu/lane_lock.py`) | Coordination primitive — prevents agent lane conflicts |

---

## API Reference

### Store a memory

```bash
curl -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -H "X-MemU-Key: your-key" \
  -d '{
    "content": "Always run migrations before deploying",
    "memory_type": "lesson",
    "agent_id": "lenny",
    "metadata": {"project": "infrastructure"}
  }'
```

### Semantic search

```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -H "X-MemU-Key: your-key" \
  -d '{
    "query": "deployment mistakes",
    "limit": 10,
    "agent_id": null,
    "memory_type": "lesson",
    "temporal_weight": 0.3
  }'
```

### RAG chat

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-MemU-Key: your-key" \
  -d '{
    "question": "What do we know about CI/CD best practices?",
    "agent_id": null
  }'
```

Legacy `X-API-Key` remains supported for backward compatibility, but `X-MemU-Key` is the canonical auth header.

### Other endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check (DB + service status) |
| `/memories/{id}` | GET | Retrieve a specific memory |
| `/memories/{id}` | DELETE | Delete a memory |
| `/memories/bulk` | POST | Bulk import from markdown/JSON |
| `/tasks` | GET | List unified tasks (filter by owner, status, project, risk, etc.) |
| `/tasks` | POST | Create a task (includes risk/refinement metadata) |
| `/tasks/{task_id}` | PATCH | Update task state/owner/risk/outcome metadata |
| `/tasks/{task_id}/review` | POST | Critical completion review decision endpoint |
| `/api/dag/{root_id}` | GET | DAG snapshot for a root prompt |
| `/api/cluster/status` | GET | NATS cluster health |
| `/api/halt` | POST | Emergency halt (God Mode) |
| `/ws/swarm` | WS | Real-time event stream |

#### Unified Task Registry scripts

- `scripts/task_registry_scanner.py` scans repos for TODO/FIXME/HACK/XXX markers and writes
  risk-scored tasks into memU's `backlog` table.
- `scripts/task_refiner_agent.py` enforces strict task refinement quality gates and
  rewrites/marks tasks needing revision.
- `scripts/task_completion_reviewer.py` validates task completion payloads before final closure.
- Risk model is inverted from confidence: **risk > 80 enters high-risk review mode**; lower
  risks can proceed.

---

## Memory Kinds and Types

fumemory uses two orthogonal fields to classify memories:

### `memory_kind` — the primary discriminator

| Kind | Description | Recall |
|------|-------------|--------|
| `evidence` | Immutable, task-bound execution proof written by OpenClaw gateways | Forensic Recall only |
| `learning` | Distilled reusable insight derived from evidence after review | Default recall |

Evidence Memory is **append-only** and cannot be content-deduped across distinct task/session/gateway events. Corrections are new records with explicit links to the original.

Learning Memory must carry source evidence IDs and a `review_status`. Reflection-generated learning starts as `proposed` and enters a six-hour Telegram review window before automatic integration.

### `memory_type` — semantic taxonomy

| Type | Use case |
|------|----------|
| `fact` | Concrete information — "The API runs on port 8000" |
| `decision` | Choices made — "We chose Postgres over Redis for persistence" |
| `lesson` | Things learned — "Always check migrations before deploy" |
| `pattern` | Recurring observations — "Users churn when onboarding takes >5 min" |
| `failure` | What went wrong — "Forgot to rotate API keys, caused 2h outage" |
| `user_action` | OpenClaw gateway or agent action record |
| `external` | External tool output or reference |
| `procedural` | Step-by-step procedure or runbook |

See `docs/MIGRATION_GUIDE.md` for how legacy rows are backfilled into `evidence` or `learning`.

---

## Memory Decay & Reinforcement

memU doesn't treat all memories equally. Recent, frequently-accessed memories score higher.

```
score = similarity × (1 - decay_rate)^days × (1 + log(access_count + 1)) × temporal_weight
```

- **Accessed memories get reinforced** — every search hit boosts the memory
- **Old unused memories decay** — configurable rate (default: 0.01/day)
- **Temporal weight** — configurable blend between pure similarity and recency

---

## Memory Chains

Link related memories into knowledge graphs:

```python
# A decision
decision = client.add("Switched from REST to GraphQL for the mobile API",
                       memory_type="decision")

# The outcome
outcome = client.add("GraphQL reduced mobile API calls by 60%",
                      memory_type="fact",
                      parent_id=decision.id)

# The lesson
lesson = client.add("GraphQL works well for mobile clients with variable data needs",
                     memory_type="lesson",
                     parent_id=decision.id)
```

---

## Multi-Machine Clustering (NATS)

memU supports dual-instance failover via NATS:

- **Primary:** local NATS instance (low latency)
- **Fallback:** Railway NATS instance (survives local outages)
- Agents connect to both — if local dies, traffic routes to Railway automatically

```bash
# Configure in .env
NATS_LOCAL_URL=nats://localhost:4222
NATS_RAILWAY_URL=nats://<railway-nats-host>:<railway-nats-port>
```

See `docs/MULTI_MACHINE.md` for full setup.

---

## Warden (Container Orchestrator)

The Warden manages container lifecycle without any LLM involvement:

- **Heartbeat watchdog** — detects crashed gateways
- **Auto-respawn** — restarts failed containers from signed NATS events
- **Warm standby pool** — instant failover, adaptive sizing based on queue depth
- **Dead Man's Switch** — emergency shutdown if watchdog goes silent
- **Security** — cryptographic signatures on all spawn requests, air-gapped sandboxes

---

## Configuration

Environment variables (see `.env.example`):

```bash
# Database
DATABASE_URL=postgresql://memu:memu@localhost:5432/memu

# Auth
MEMU_API_KEY=your-secret-key
MEMU_API_URL=http://127.0.0.1:8000  # scripts/hooks target this base URL
# Legacy alias still accepted by some older helpers: MEMU_BASE_URL

# Embeddings (any OpenAI-compatible endpoint)
# Canonical variable: EMBEDDING_API_BASE (EMBEDDING_BASE_URL is a deprecated alias)
OPENAI_API_KEY=          # required when EMBEDDING_API_BASE points to OpenAI

# Default (OpenAI-compatible):
EMBEDDING_API_BASE=https://api.openai.com
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMS=1536

# Decay
DECAY_RATE=0.01
DEDUP_THRESHOLD=0.95

# Embedding provider for production (do NOT point to local laptop from Railway)
# Option A: OpenAI-compatible API (default)
# OPENAI_API_KEY=sk-...
# EMBEDDING_API_BASE=https://api.openai.com
# EMBEDDING_MODEL=text-embedding-3-small
# EMBEDDING_DIMS=1536

# Option B: hosted Ollama service on Railway (self-hosted path — set all three)
# EMBEDDING_API_BASE=http://<railway-ollama-service>.railway.internal:11434
# EMBEDDING_MODEL=nomic-embed-text
# EMBEDDING_DIMS=768

# NATS clustering
NATS_LOCAL_URL=nats://localhost:4222
NATS_RAILWAY_URL=nats://<railway-nats-host>:<railway-nats-port>

# Temporal (only required for /memories/async and /search/async)
TEMPORAL_HOST=<temporal-host>:7233
TEMPORAL_TLS=false
```

---

## Railway Notes

- Railway injects `PORT` for HTTP services; `memu.api` already honors it.
- Do **not** hardcode `*.proxy.rlwy.net` hosts into committed config. Use env vars per deploy.
- Core API boot requires `DATABASE_URL` and `MEMU_API_KEY`.
- `NATS_RAILWAY_URL` is optional for API boot, but **required for Federation Readiness**.
- `TEMPORAL_HOST`/`TEMPORAL_TLS` are only required for `/memories/async` and `/search/async`.

### Verification commands

```bash
# Core API Readiness (API + Postgres + auth + write + recall — no NATS or Temporal needed)
python scripts/verify_deployment.py --api-url <url>

# Federation Readiness (Core API + NATS + idempotency replay + searchable proof)
python scripts/verify_deployment.py --api-url <url> --check-federation

# Async Readiness (Core API + Temporal async endpoints)
python scripts/verify_deployment.py --api-url <url> --check-async

# Emit a machine-readable proof artifact (secrets redacted)
python scripts/verify_deployment.py --api-url <url> --proof-out proof-core.json
python scripts/verify_deployment.py --api-url <url> --check-federation --proof-out proof-federation.json
```

- Add `--check-federation` only when `nats-jetstream` is deployed and `NATS_RAILWAY_URL` is set.
- Add `--check-async` only when `temporal-worker` is deployed and healthy.
- For local/CI NATS lane-lock evidence, run `./scripts/verify_nats_lane_lock.sh` or `./scripts/verify_baseline.sh`.

See `docs/railway-readiness.md` for the full pre-deploy checklist and `docs/OPENCLAW_INTEGRATION.md` for OpenClaw hook wiring.

---

## Repository Structure

```text
fumemory/
├── memu/
│   ├── api.py              # FastAPI application
│   ├── models.py           # Pydantic schemas
│   ├── client.py           # Python client library
│   ├── decay.py            # Decay/reinforcement scoring
│   ├── cluster.py          # NATS dual-instance failover
│   ├── warden.py           # Container orchestration
│   ├── warden_runtime.py   # Warden heartbeat runtime
│   ├── ws_bridge.py        # WebSocket event bridge
│   ├── bridge_ledger.py    # In-memory DAG + stats
│   ├── lane_lock.py        # Agent coordination locks
│   ├── hardening.py        # Failure mode mitigations
│   ├── schema.sql          # PostgreSQL + pgvector schema
│   └── events_schema.sql   # Event sourcing schema
├── ui/                     # Glass Box dashboard (React + Vite)
├── warden/                 # Warden container configs
├── infra/                  # NATS configs (local + Railway)
├── scripts/                # Utilities (DAG migration, etc.)
├── tests/                  # Test suite
├── docs/                   # API docs, multi-machine setup
├── docker-compose.yml      # Full stack: Postgres + NATS + API + Bridge
├── Dockerfile              # API server container
└── Dockerfile.gateway      # Gateway container
```

---

## Roadmap

- [x] Core API (add, search, delete, chat)
- [x] Multi-agent namespacing
- [x] Memory types & chains
- [x] Decay & reinforcement
- [x] Deduplication
- [x] NATS multi-machine clustering
- [x] Warden container orchestration
- [x] WebSocket bridge + Glass Box UI
- [x] Bridge ledger (DAG, events, trajectories)
- [x] Security hardening (10 failure modes mitigated)
- [x] Evidence Memory / Learning Memory separation (`memory_kind`)
- [x] Canonical synchronous evidence write path (`/api/v1/memu/add`)
- [x] Idempotency by `(tenant_id, idempotency_key)` with 409 replay detection
- [x] Default learning recall (accepted Learning Memory only)
- [x] Forensic Recall with task/session/gateway provenance
- [x] Reflection Worker + Reflection Review Queue (six-hour window)
- [x] Compact Telegram reflection notices
- [x] Core API Readiness / Federation Readiness / Async Readiness verification gates
- [x] Versioned embedding contract (`EMBEDDING_API_BASE` / `EMBEDDING_MODEL` / `EMBEDDING_DIMS`)
- [x] Async Memory Workflow parity tests (Temporal optional)
- [x] Memory Action Eval (multi-session behavioral proof)
- [ ] LangChain / LlamaIndex integration
- [ ] Multi-tenant mode
- [ ] Kubernetes Helm chart
- [ ] PyPI package release

See also:
- `CONTRIBUTING.md`
- `SECURITY.md`
- `CONTEXT.md` — canonical domain language (Memory Evidence Plane, Evidence Memory, Learning Memory, etc.)
- `docs/OPENCLAW_INTEGRATION.md` — OpenClaw hook wiring, Telegram reflection review, Forensic Recall
- `docs/MIGRATION_GUIDE.md` — legacy memory classification and embedding versioning
- `docs/railway-readiness.md` — Railway pre-deploy checklist and readiness gates
- `docs/CROSS_GATEWAY_NATS_FEDERATION.md`
- `docs/GATEWAY_FEDERATION_ROLLOUT_CHECKLIST.md`
- `docs/MULTI_MACHINE.md`
- `scripts/gateway_federation_smoke.py`
- `memu/policy/rules/jetstream_authz.rego`

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). We welcome PRs — especially for integrations, new memory types, and dashboard work.

---

## License

MIT — do whatever you want with it.

---

<div align="center">

**Built by [Protelynx](https://protelynx.ai)** — we build multi-agent AI systems.

</div>
