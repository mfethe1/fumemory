<div align="center">

# memU

### Free, open-source shared memory for AI agents.

**Stop paying for memory. Your agents deserve better.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![PostgreSQL 16+](https://img.shields.io/badge/postgres-16+-336791.svg)](https://postgresql.org)

[Quick Start](#quick-start) · [Features](#features) · [Architecture](#architecture) · [API Reference](#api-reference) · [Contributing](CONTRIBUTING.md)

</div>

---

## Why pay for memory?

SuperMemory charges you monthly. Mem0 wants your data on their servers. Zep locks you into their cloud.

**memU is free. Forever. Run it on your own Postgres.**

Your agents' memories belong to you — not a SaaS vendor.

---

## Features

**LLM wiki layer (solo-user friendly)**
- **Obsidian-compatible markdown vault** — every memory is a plain `.md` with YAML frontmatter and `[[wikilinks]]`; open the folder in Obsidian for free graph view + backlinks
- **Pluggable storage** — markdown-only / SQLite / Postgres + pgvector; swap backends by changing `MEMU_STORAGE_DSN`, no code changes
- **Python codebase ingester** — `memu ingest code <repo>` materializes every top-level class, function, method, and module as an addressable slug, with typed outbound links for imports and same-module calls
- **Hybrid retrieval** — FTS + vector + graph fused via Reciprocal Rank Fusion, capability-introspected per backend (no config)
- **Recursive-LM orchestrator** — decomposes a task, retrieves slugs, recursively summarizes; sub-agents receive slug citations, not raw file contents, so context stays small
- **Wiki sync daemon** — `memu sync watch` keeps the index fresh as Obsidian / VS Code / any editor writes to your vault (polling, debounced, no C dependency)
- **MCP server + Claude Code plugin + editor shims** — one `python -m memu.mcp.server` serves Claude Code, Cursor, Zed, Continue, Windsurf, Aider, Codex, and Gemini from a single implementation

**Swarm-scale (team/enterprise)**
- **Semantic vector search** — pgvector-powered similarity search over all memories
- **Multi-agent namespacing** — agents share one memory pool, search across all or filter by agent
- **Temporal awareness** — recency-weighted search, not just cosine similarity
- **Memory decay & reinforcement** — frequently accessed memories get boosted, stale ones fade
- **Typed memories** — fact, decision, lesson, pattern, failure — structured taxonomy
- **Memory chains** — link related memories (decision → outcome → lesson learned)
- **Deduplication** — detects near-duplicates on upsert, merges instead of duplicating
- **Bulk import** — ingest markdown files, knowledge bases, existing memory stores
- **RAG chat** — conversational queries over your entire knowledge base
- **NATS mesh** — multi-machine clustering with local + Railway failover
- **Warden** — container orchestration with heartbeat watchdog and Dead Man's Switch
- **Glass Box UI** — real-time WebSocket dashboard for swarm observability
- **One command to run** — `docker compose up` and you're done

---

## Two ways to run memU

memU scales from a solo researcher's laptop to a multi-agent swarm on Postgres without changing a line of your agent code. Pick the tier that matches your needs; the `StorageBackend` abstraction means the same memory code runs in all of them.

| Tier | DSN | Use case | Dependencies |
|---|---|---|---|
| **Tier 0 — markdown** | `file://~/vault` | Solo user, pure Obsidian | stdlib only |
| **Tier 1 — SQLite** | `sqlite:///path/index.db` | Solo user + fast search | stdlib only |
| **Tier 2 — Postgres** | `postgres://…` | Team, shared memory | Postgres + pgvector |
| **Tier 3 — Postgres + AGE + RLS** | `postgres://…` | Enterprise swarm | Postgres + pgvector + Apache AGE |

```bash
# Tier 1 quick start — ~30 seconds, no Docker
pip install -e .
memu init ~/my-vault
export MEMU_STORAGE_DSN=sqlite:///~/my-vault/.memu/index.db
memu ingest code /path/to/your/repo
memu solve "fix the FooParser bug"
```

```bash
# Tier 2/3 quick start — team / enterprise
docker compose up -d
export MEMU_STORAGE_DSN=postgres://memu:memu@localhost:5432/memu
# Existing FastAPI routes under :8000 continue to serve the swarm stack
```

---

## LLM wiki layer

Inspired by Karpathy's "LLM wiki" idea: the LLM both writes and reads a shared knowledge base of notes, code symbols, and research papers, connected by typed wikilinks. memU's implementation:

### Vault layout (Obsidian-native)

```
vault/
  notes/<slug>.md                         # handwritten + LLM-generated notes
  code/<lang>/<module>/<symbol>.md        # auto-materialized by `memu ingest code`
  papers/<arxiv-id>.md                    # (future: `memu ingest paper`)
  tasks/<task-id>.md                      # agent task state
  _index/master.md                        # LLM-maintained TOC
  .memu/index.db                          # SQLite index (Tier 1)
```

Every node has YAML frontmatter (`id`, `slug`, `kind`, `tags`, `links`, `source`) and a markdown body with `[[wikilinks]]`. Obsidian renders graph view + backlinks with zero plugins.

### Recursive retrieval (RLM orchestrator)

```python
from memu.rlm import Orchestrator, RLMContext
from memu.storage import get_backend

backend = get_backend()
await backend.init()
orch = Orchestrator(backend)  # auto-uses HybridRetriever (FTS + vector + graph)

result = await orch.solve("fix the FooParser bug", RLMContext(max_depth=2, k=6))
print(result.slugs)   # -> ['code/memu/parsers/FooParser', 'code/memu/parsers/_private', ...]
print(result.answer)  # slug-cited synthesis — NOT raw bodies
```

Sub-agents get slugs, not full file contents — context stays small as you scale.

### MCP / plugin surface

One `python -m memu.mcp.server` process exposes:

- `wiki_search` / `wiki_read` / `wiki_write` / `wiki_link` / `wiki_backlinks`
- `rlm_solve` — recursive orchestration
- `wiki_ingest_code` — codebase materialization

All served over MCP stdio, so **one implementation** powers the Claude Code plugin bundle (`/memu:wiki-search`, `/memu:ingest-code`, `/memu:rlm-investigate`, `/memu:vault-sync`, `/memu:fix-with-context`), Cursor, Zed, Continue, Windsurf, Aider, Codex CLI, and Gemini CLI. Adding a new editor is a `mcp.json`, not a new integration.

```bash
# In Claude Code
/plugin marketplace add mfethe1/fumemory
/plugin install memu
```

See [`plugins/README.md`](plugins/README.md) for per-editor install paths and [`docs/UPSTREAM_PORT_BACKLOG.md`](docs/UPSTREAM_PORT_BACKLOG.md) for the roadmap of features we're porting from upstream NevaMind-AI/memU.

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

### Solo user (markdown + SQLite, no Docker)

```bash
git clone https://github.com/mfethe1/fumemory.git
cd fumemory
pip install -e .

memu init ~/my-vault
export MEMU_STORAGE_DSN=sqlite:///~/my-vault/.memu/index.db

# Seed the wiki from your codebase
memu ingest code /path/to/your/repo --exclude 'tests/**'

# Search, list links, run the RLM orchestrator
memu search "FooParser"
memu links code/pkg/foo/FooParser
memu solve "where does FooParser handle errors"

# Open the folder in Obsidian for graph + backlinks (optional)
obsidian ~/my-vault
```

### Agent integration (MCP — works in Claude Code, Cursor, Zed, Continue, Aider, Codex, Gemini)

```bash
# Claude Code
/plugin marketplace add mfethe1/fumemory
/plugin install memu

# Cursor, Zed, Continue, Windsurf, Aider, Codex CLI, Gemini CLI:
# see plugins/README.md — each is a single config file pointing at
# `python -m memu.mcp.server`
```

### Team / swarm (Docker)

```bash
git clone https://github.com/mfethe1/fumemory.git
cd fumemory
docker compose up -d
```

memU is running at `http://localhost:8000`.

### Python client (legacy Postgres API)

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

### From source (Postgres + NATS stack)

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

```text
Your Agents (Winnie, Rosie, Lenny, Macklemore, ...)
     |            |            |            |
     +----------- + -----------+------------+
                   |
                   v
            memU Python Client
                   |
                   v
     +-----------------------------+
     |        memU API Server      |
     |     (FastAPI + asyncpg)     |
     |                             |
     |  /memories   POST - store   |
     |  /search     POST - query   |
     |  /chat       POST - RAG     |
     |  /health     GET  - status  |
     +-----------------------------+
         |                   |
         v                   v
   +-----------+     +--------------+
   | PostgreSQL|     |     NATS     |
   | + pgvector|     |   Cluster    |
   |           |     |              |
   | embeddings|     | local + rail |
   | metadata  |     | failover     |
   | chains    |     +--------------+
   | decay     |            |
   +-----------+            v
                    +--------------+
                    |   Warden     |
                    | (container   |
                    |  lifecycle)  |
                    +--------------+
                            |
                            v
                    +--------------+
                    |  WS Bridge   |
                    | (Glass Box   |
                    |  real-time   |
                    |  dashboard)  |
                    +--------------+
```

### Components

| Component | What it does |
|-----------|-------------|
| **memU API** (`memu/api.py`) | FastAPI server — memory CRUD, semantic search, RAG chat |
| **PostgreSQL + pgvector** | Storage layer — embeddings, metadata, memory chains, decay scores |
| **NATS Cluster** (`memu/cluster.py`) | Multi-machine mesh — local primary + Railway fallback |
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

## Memory Types

| Type | Use case |
|------|----------|
| `fact` | Concrete information — "The API runs on port 8000" |
| `decision` | Choices made — "We chose Postgres over Redis for persistence" |
| `lesson` | Things learned — "Always check migrations before deploy" |
| `pattern` | Recurring observations — "Users churn when onboarding takes >5 min" |
| `failure` | What went wrong — "Forgot to rotate API keys, caused 2h outage" |

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
OPENAI_API_KEY=          # optional, for OpenAI
EMBEDDING_BASE_URL=http://localhost:11434  # Ollama default
EMBEDDING_MODEL=qwen3-embedding
EMBEDDING_DIMS=4096

# Decay
DECAY_RATE=0.01
DEDUP_THRESHOLD=0.95

# Embedding provider for production (do NOT point to local laptop from Railway)
# Option A: OpenAI-compatible API
# OPENAI_API_KEY=sk-...
# EMBEDDING_BASE_URL=https://api.openai.com/v1  # or compatible endpoint
# EMBEDDING_MODEL=text-embedding-3-small
# EMBEDDING_DIMS=1536

# Option B: hosted Ollama service on Railway (recommended for self-hosted path)
EMBEDDING_BASE_URL=http://<railway-ollama-service>.railway.internal:11434
EMBEDDING_MODEL=qwen3-embedding
EMBEDDING_DIMS=4096

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
- `NATS_RAILWAY_URL` is optional for API boot, but required for mesh features.
- `TEMPORAL_HOST`/`TEMPORAL_TLS` are only required if you want `/memories/async` and `/search/async`.
- Use `python scripts/verify_deployment.py --api-url <url>` for core API checks.
- Add `--check-async` only when the Temporal service and worker are deployed and healthy.
- For local/CI evidence on NATS lane locking, run `./scripts/verify_nats_lane_lock.sh` (JetStream-backed) or `./scripts/verify_baseline.sh`.

See `docs/railway-readiness.md` for the full pre-deploy checklist.

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
- [ ] LangChain / LlamaIndex integration
- [ ] Memory compression (summarize old memories)
- [ ] Multi-tenant mode
- [ ] Kubernetes Helm chart
- [ ] PyPI package release

See also:
- `CONTRIBUTING.md`
- `SECURITY.md`
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
