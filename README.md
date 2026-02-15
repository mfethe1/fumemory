<![CDATA[<div align="center">

# 🧠 memU

### Free, open-source shared memory for AI agents.

**Stop paying for memory. Your agents deserve better.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://python.org)
[![PostgreSQL 16+](https://img.shields.io/badge/postgres-16+-336791.svg)](https://postgresql.org)

[Quick Start](#quick-start) · [Features](#features) · [Why memU?](#why-memu) · [API Reference](#api-reference) · [Contributing](CONTRIBUTING.md)

</div>

---

## Why pay for memory?

SuperMemory charges you monthly to store and search your AI's memories. Mem0 wants your data on their servers. Zep locks you into their cloud.

**memU is free. Forever. Run it on your own Postgres.**

Your agents' memories belong to you — not a SaaS vendor.

---

## Features

- 🔍 **Semantic vector search** — pgvector-powered similarity search over all memories
- 🤖 **Multi-agent namespacing** — multiple agents share one memory pool, search across all or filter by agent
- ⏰ **Temporal awareness** — recency-weighted search, not just cosine similarity
- 📉 **Memory decay & reinforcement** — frequently accessed memories get boosted, stale ones fade
- 🏷️ **Typed memories** — fact, decision, lesson, pattern, failure — structured taxonomy
- 🔗 **Memory chains** — link related memories (decision → outcome → lesson learned)
- 🔄 **Deduplication** — detects near-duplicate memories on upsert, merges instead of duplicating
- 📥 **Bulk import** — ingest markdown files, knowledge bases, existing memory stores
- 💬 **RAG chat** — conversational queries over your entire knowledge base
- 🐳 **One command to run** — `docker compose up` and you're done

---

## Why memU?

| Feature | memU | SuperMemory | Mem0 | Zep |
|---------|------|-------------|------|-----|
| **Price** | **Free forever** | $29+/mo | Usage-based | $99+/mo |
| **Self-hosted** | ✅ | ❌ | Partial | Partial |
| **Multi-agent** | ✅ Native | ❌ | ❌ | ❌ |
| **Memory decay** | ✅ | ❌ | ❌ | ❌ |
| **Memory types** | ✅ 5 types | ❌ | ❌ | Basic |
| **Memory chains** | ✅ | ❌ | ❌ | ❌ |
| **Deduplication** | ✅ | ❌ | Basic | ❌ |
| **Temporal weighting** | ✅ | ❌ | ❌ | ✅ |
| **Your data stays yours** | ✅ | ❌ | ❌ | ❌ |
| **Open source** | ✅ MIT | ❌ | Partial | ❌ |

---

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/protelynx/memu.git
cd memu
docker compose up -d
```

That's it. memU is running at `http://localhost:8000`.

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

### From source

```bash
git clone https://github.com/protelynx/memu.git
cd memu
pip install -e .

# Start Postgres with pgvector
docker compose up -d postgres

# Run the API
memu serve
```

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   Your Agents                    │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐       │
│  │Agent1│  │Agent2│  │Agent3│  │Agent4│  ...   │
│  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘       │
│     │         │         │         │             │
│     └─────────┴────┬────┴─────────┘             │
│                    │                             │
│              ┌─────▼─────┐                       │
│              │ memU Client│  (Python / REST)     │
│              └─────┬─────┘                       │
└────────────────────┼────────────────────────────┘
                     │
              ┌──────▼──────┐
              │  memU API    │  FastAPI
              │  /memories   │  /search
              │  /chat       │  /health
              └──────┬──────┘
                     │
              ┌──────▼──────┐
              │  PostgreSQL  │
              │  + pgvector  │
              │              │
              │ ┌──────────┐ │
              │ │embeddings│ │
              │ │metadata  │ │
              │ │chains    │ │
              │ │decay     │ │
              │ └──────────┘ │
              └──────────────┘
```

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

memU doesn't treat all memories equally. Recent, frequently-accessed memories score higher.

```
score = similarity × (1 - decay_rate)^days_since_created × (1 + log(access_count + 1)) × temporal_weight
```

- **Accessed memories get reinforced** — every search hit boosts the memory
- **Old unused memories decay** — configurable decay rate (default: 0.01/day)
- **Temporal weight** — configurable blend between pure similarity and recency

---

## Memory Chains

Link related memories to build knowledge graphs:

```python
# A decision was made
decision = client.add("Switched from REST to GraphQL for the mobile API", 
                       memory_type="decision")

# Later, we learned the outcome
outcome = client.add("GraphQL reduced mobile API calls by 60%",
                      memory_type="fact", 
                      parent_id=decision.id)

# And distilled a lesson
lesson = client.add("GraphQL works well for mobile clients with variable data needs",
                     memory_type="lesson",
                     parent_id=decision.id)
```

---

## Configuration

```yaml
# memu.yaml
database:
  url: postgresql://user:pass@localhost:5432/memu
  
embedding:
  model: text-embedding-3-small  # or any OpenAI-compatible endpoint
  dimensions: 1536
  
decay:
  rate: 0.01          # per day
  min_score: 0.1      # floor — memories never fully disappear
  
deduplication:
  enabled: true
  threshold: 0.95     # cosine similarity threshold for duplicate detection
  
search:
  default_limit: 10
  temporal_weight: 0.3  # 0 = pure similarity, 1 = pure recency
```

---

## Roadmap

- [x] Core API (add, search, delete)
- [x] Multi-agent namespacing
- [x] Memory types & chains
- [x] Decay & reinforcement
- [x] Deduplication
- [ ] Web dashboard
- [ ] Webhook integrations (Slack, Discord)
- [ ] LangChain / LlamaIndex integration
- [ ] Memory compression (summarize old memories to save space)
- [ ] Multi-tenant mode (multiple orgs on one instance)
- [ ] Kubernetes Helm chart

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). We welcome PRs — especially for integrations, new memory types, and dashboard work.

---

## License

MIT — do whatever you want with it.

---

<div align="center">

**Built by [Protelynx](https://protelynx.ai)** — we build multi-agent AI systems.

Need help building agent infrastructure for your team? **[protelynx.ai](https://protelynx.ai)**

</div>
]]>