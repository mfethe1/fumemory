# Railway Deployment Guide — fumemory API

## Readiness tiers

fumemory deploys as up to five named services. Each tier is a strict superset of the previous:

| Tier | Required services | Verify with |
|------|------------------|-------------|
| **Core API Readiness** | `api` + `postgres-pgvector` | `python scripts/verify_deployment.py --api-url <url>` |
| **Federation Readiness** | Core API + `nats-jetstream` | `python scripts/verify_deployment.py --api-url <url> --check-federation` |
| **Async Readiness** | Core API + `temporal-worker` | `python scripts/verify_deployment.py --api-url <url> --check-async` |

Missing NATS does **not** fail Core API Readiness. Missing Temporal does **not** fail Core API or Federation Readiness. Add optional services only when you need them.

## Prerequisites
- Railway account + `railway` CLI installed and logged in
- PostgreSQL service (with pgvector) provisioned in the same Railway project
- (Optional) NATS service for Federation Readiness
- (Optional) Temporal service for async workflow endpoints

## Deploy Steps

### 1. Link project
```bash
railway link  # pick your project
```

### 2. Set required environment variables
```bash
# Required
railway variables set DATABASE_URL="<postgresql-url-from-railway-postgres>"
railway variables set MEMU_API_KEY="<strong-random-key>"

# Optional — enable semantic embeddings via OpenAI or a compatible provider
# railway variables set OPENAI_API_KEY="<key>"
# railway variables set EMBEDDING_API_BASE="https://api.openai.com"
# railway variables set EMBEDDING_MODEL="text-embedding-3-small"
# railway variables set EMBEDDING_DIMS="1536"

# Optional — NATS event streaming
# railway variables set NATS_RAILWAY_URL="nats://<host>:<port>"
```

### 3. Deploy
```bash
railway up
```

Railway auto-detects `railway.toml` → builds via `Dockerfile` → starts with `python -m memu.api`.

### 4. Verify health
```bash
curl https://<your-app>.up.railway.app/health
# Expected: {"status":"healthy","version":"0.1.0"}
```

### 5. Run the appropriate readiness gate

```bash
# Core API Readiness (always run first)
python scripts/verify_deployment.py --api-url https://<your-app>.up.railway.app

# Federation Readiness (add only when nats-jetstream is deployed)
python scripts/verify_deployment.py --api-url https://<your-app>.up.railway.app --check-federation

# Emit proof artifact (secrets redacted)
python scripts/verify_deployment.py --api-url https://<your-app>.up.railway.app --proof-out proof-core.json
```

Deploy the NATS service from its own Railway config:
```bash
railway up infra/railway-nats --path-as-root --service nats-jetstream
```

Deploy the Temporal worker from its own Railway config:
```bash
railway up infra/railway-temporal-worker --path-as-root --service temporal-worker
```

The Temporal worker config is self-contained under `infra/railway-temporal-worker/`.
Because `--path-as-root` uploads only that directory, its Dockerfile fetches the
fumemory source from the configured Git ref during image build instead of relying
on temporary staging folders.

## Environment Variable Reference

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | ✅ | `postgresql://memu:memu@localhost:5432/memu` | Must be postgres+pgvector |
| `MEMU_API_KEY` | ✅ | `memu-dev-key` | Change in production |
| `OPENAI_API_KEY` | Optional | — | For OpenAI embeddings / chat |
| `EMBEDDING_API_BASE` | Optional | `https://api.openai.com` | Embedding provider base URL. `EMBEDDING_BASE_URL` is a deprecated alias. |
| `EMBEDDING_MODEL` | Optional | `text-embedding-3-small` | Embedding model name |
| `EMBEDDING_DIMS` | Optional | `1536` | Must match model and schema dims |
| `NATS_RAILWAY_URL` | Optional | — | Event streaming; API works without it |
| `MEMU_ENABLE_STARTUP_NATS` | Optional | `0` | Set to `1` only for Federation Readiness after NATS is healthy |
| `NATS_LOCAL_URL` | Optional | — | Only for local dev |
| `TEMPORAL_HOST` | Optional | — | Required for async workflow routes only |
| `LOG_LEVEL` | Optional | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `DEDUP_THRESHOLD` | Optional | `0.95` | Cosine similarity for dedup |

## Health Contract
- **Endpoint:** `GET /health`
- **Healthy response:** `200 {"status":"healthy","version":"0.1.0"}`
- **Unhealthy:** `503` — means DB connection is down
- Railway healthcheck uses `/health` with 90s timeout (see `railway.toml`)

## Notes
- NATS is optional — API starts and operates fully without it (events are silently skipped). Required only for Federation Readiness.
- Temporal is optional — async routes return 503 if `TEMPORAL_HOST` is not set. Required only for Async Readiness.
- Production embedding defaults are `text-embedding-3-small` / `1536` via `EMBEDDING_API_BASE`. Set `OPENAI_API_KEY` to enable them.
- `EMBEDDING_BASE_URL` is a deprecated alias for `EMBEDDING_API_BASE`. Use `EMBEDDING_API_BASE` in all new config.
- Embedding dimension changes require an additive migration (new vector column or embedding version + background reindex). Never drop and recreate production embedding columns as a migration strategy.
- Migrations run automatically on startup via `memu/migrations/`.
- See `docs/railway-readiness.md` for the complete pre-deploy checklist and `docs/MIGRATION_GUIDE.md` for embedding versioning guidance.
