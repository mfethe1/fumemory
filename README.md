# memU (FuMemory)

Open-source shared memory + coordination primitives for multi-agent systems.

Run locally with Postgres/pgvector, or integrate into production services (like Railway-hosted APIs) with contract checks and guardrails.

---

## What this project includes

- Persistent memory API (`/memories`, `/search`, `/chat`)
- Semantic retrieval + metadata filtering
- Agent-aware memory workflows
- Coordination components for multi-agent operation
- Production validation scripts and contract checks

---

## New system (current)

memU is used in two modes:

1. **OSS / local mode** (this repo)
   - Standalone memU API on Docker/Postgres
2. **Integrated production mode**
   - memU routes mounted under service APIs (example: `/api/v1/memu/*`)
   - Guarded by auth contract + runtime checks

### Production contract notes

- Common integrated routes:
  - `POST /api/v1/memu/search`
  - `POST /api/v1/memu/search-text`
  - `POST /api/v1/memu/add`
  - `GET /api/v1/memu/health`
- Canonical auth header policy:
  - Primary: `X-MemU-Key`
  - Compatibility (legacy): `X-API-Key`
  - If both are present, `X-MemU-Key` is authoritative.
  - Missing auth returns `401 Missing authentication credentials`.
  - Invalid auth returns `401 Invalid X-MemU-Key`.
- `add` payload expects `content` (not legacy `text`)
- Integration checks typically run through:
  - `scripts/memu_contract_check.sh`
  - `scripts/lenny_cron_guard.sh`
  - `tools/memu_auth_diagnostics.py`

---

## Quick start (local)

```bash
git clone https://github.com/mfethe1/fumemory.git
cd fumemory
docker compose up -d
```

Service: `http://localhost:8000`

### Basic API calls

```bash
# Health
curl -s http://localhost:8000/health

# Add memory
curl -s -X POST http://localhost:8000/memories \
  -H "Content-Type: application/json" \
  -H "X-API-Key: memu-dev-key" \
  -d '{
    "content":"Always rotate keys after auth incidents",
    "memory_type":"lesson",
    "agent_id":"macklemore",
    "metadata":{"source":"incident"}
  }'

# Search
curl -s -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: memu-dev-key" \
  -d '{"query":"auth incident","limit":5}'
```

---

## API surface (OSS mode)

- `GET /health`
- `POST /memories`
- `GET /memories/{id}`
- `DELETE /memories/{id}`
- `POST /search`
- `POST /chat`
- `POST /memories/bulk`

---

## Coordination layer (Swarm/ops components)

The repo also includes coordination/runtime modules used in multi-agent setups:

- `lane_lock.py` — lane ownership + contention control
- `session_hook.py` — idempotent session memory write/read hook
- `temporal_workflow.py` — durable health/repair workflow with persisted state
- `warden_runtime.py` — watchdog runtime loop
- `bridge_ledger.py` — append-only event ledger
- `ws_bridge.py` — websocket/NATS bridge path
- `cluster.py`, `boot.py`, `hardening.py` — startup + reliability pieces

---

## Repo structure

```text
fumemory/
├── memu/                 # API + memory + coordination modules
├── docs/                 # docs/runbooks
├── templates/            # templates/examples
├── docker-compose.yml    # local stack
├── pyproject.toml        # package config
└── README.md
```

---

## Development

```bash
pip install -e .
memu serve
```

See also:
- `CONTRIBUTING.md`
- `SECURITY.md`

---

## License

MIT
