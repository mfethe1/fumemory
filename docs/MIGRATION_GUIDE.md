# Migration Guide

This guide covers two migration topics introduced by the Memory Evidence Plane architecture:

1. **Legacy memory classification** — how existing rows are backfilled into `evidence` or `learning`
2. **Embedding versioning** — how to change embedding models or dimensions without destroying existing searchable memory

---

## Legacy memory classification

fumemory now uses `memory_kind` as the primary discriminator:

| `memory_kind` | Recall mode | Description |
|---------------|-------------|-------------|
| `evidence` | Forensic Recall only | Immutable, task-bound execution proof |
| `learning` | Default recall | Distilled reusable insight |

Existing rows that predate `memory_kind` are backfilled by the following deterministic rules.

### Backfill rules

**Rows that become `evidence`:**

- `memory_type` is `user_action`, `external`, or a search/tool record
- Row metadata contains any of `task_id`, `session_id`, `gateway_id`, `agent_id`, `event_type`
- Row content matches OpenClaw gateway execution patterns (tool calls, hook outputs, audit records)

**Rows that become `learning`:**

- `memory_type` is `lesson`, `decision`, `pattern`, `procedural`, `fact`, `reflection`, `plan`, or `goal`
- These become `learning` with `review_status = 'legacy'`

**Ambiguous `observation` rows:**

- If the row lacks OpenClaw execution metadata (no `task_id`, `session_id`, `gateway_id`): becomes `learning` with `review_status = 'legacy'`
- If the row has OpenClaw execution metadata: becomes `evidence`

### Default recall during migration

Default recall includes both `accepted` and `legacy` Learning Memory so historical useful memory does not disappear. Operators can later tighten to `accepted`-only after enough legacy review coverage exists.

Evidence Memory classified from legacy rows is excluded from default recall but remains available through Forensic Recall.

### Running the backfill

```bash
# Apply the memory_kind backfill migration
python scripts/apply_migration_003.py
```

Verify the result:

```bash
# Count evidence vs learning after backfill
python -c "
import asyncio, os, asyncpg
async def run():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    rows = await conn.fetch(\"SELECT memory_kind, review_status, COUNT(*) FROM memories GROUP BY 1,2 ORDER BY 1,2\")
    for r in rows:
        print(dict(r))
asyncio.run(run())
"
```

---

## Embedding versioning

### Canonical embedding environment variables

| Variable | Role | Example |
|----------|------|---------|
| `EMBEDDING_API_BASE` | **Canonical** embedding provider base URL | `https://api.openai.com` |
| `EMBEDDING_MODEL` | Embedding model name | `text-embedding-3-small` |
| `EMBEDDING_DIMS` | Vector dimensions — **must match active schema** | `1536` |
| `EMBEDDING_BASE_URL` | **Deprecated alias** for `EMBEDDING_API_BASE` | — |

`EMBEDDING_BASE_URL` is accepted for one migration window only. It is logged as a compatibility warning at startup. Remove it from all configuration once `EMBEDDING_API_BASE` is set.

### Production defaults

Unless an Ollama service is explicitly provisioned, Railway deployments default to:

```bash
EMBEDDING_API_BASE=https://api.openai.com
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMS=1536
```

Set `OPENAI_API_KEY` to enable OpenAI-hosted embeddings.

For a hosted Ollama embedding service on Railway:

```bash
EMBEDDING_API_BASE=http://<railway-ollama-service>.railway.internal:11434
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMS=768
```

### Why dimensions must match the schema

fumemory stores embedding version alongside every vector-producing record. At startup, if `EMBEDDING_DIMS` does not match the active vector schema or embedding version, the service fails loud. This protects against silent semantic recall degradation — the exact failure mode documented in `docs/adr/0003-versioned-embedding-contract.md` and the Memory Action Eval (`tests/test_memory_action_eval.py`).

### Changing embedding models or dimensions

**Destructive migrations (drop + recreate the embedding column) are rejected.** They erase searchable memory and make Railway deploys unsafe.

The required approach is additive:

1. Add a new vector column or create a new embedding version table.
2. Start writing new embeddings to the new column/version for incoming records.
3. Background-reindex existing records into the new column/version.
4. Update recall routing to prefer the new embedding version once reindex is complete.
5. Retain the old column/version for Forensic Recall of historical evidence.

```sql
-- Example: add a new embedding version column
ALTER TABLE memories ADD COLUMN embedding_v2 vector(3072);
-- Populate via background reindex job before switching recall routing
```

### Verifying embedding configuration

```bash
# Check that EMBEDDING_DIMS matches the active schema
python scripts/verify_deployment.py --api-url <url>
# A dimension mismatch causes the recall check to fail — fix EMBEDDING_DIMS before deploying

# Run embedding contract tests
python -m pytest tests/test_embedding_contract.py -v
```

The embedding contract tests cover:

- `EMBEDDING_API_BASE` is canonical; `EMBEDDING_BASE_URL` triggers a compatibility warning
- Model/dimension mismatch fails verification
- Additive embedding-version migration preserves existing vectors

---

## Verification checklist after migration

```bash
# 1. Core API Readiness (includes recall smoke)
python scripts/verify_deployment.py --api-url <url>

# 2. Embedding contract
python -m pytest tests/test_embedding_contract.py -v

# 3. Recall contracts (learning vs evidence separation)
python -m pytest tests/test_recall_contracts.py -v

# 4. Schema contracts (canonical write validation)
python -m pytest tests/test_schema_contract.py -v

# 5. Full pre-deploy smoke
pytest -q tests/test_gateway_leases_api.py tests/test_search_upgrade.py tests/test_memu_auto_write_nats_fallback.py
```

See `docs/railway-readiness.md` for the complete pre-deploy checklist.
