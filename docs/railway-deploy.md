# Railway Deployment Guide — memU API

## Prerequisites
- Railway account + `railway` CLI installed and logged in
- PostgreSQL service (with pgvector) provisioned in the same Railway project
- (Optional) NATS service for event streaming

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

# Optional — use fastembed (local, no ollama) if not set
# railway variables set OPENAI_API_KEY="<key>"
# railway variables set EMBEDDING_BASE_URL="https://api.openai.com/v1"
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

## Environment Variable Reference

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | ✅ | `postgresql://memu:memu@localhost:5432/memu` | Must be postgres+pgvector |
| `MEMU_API_KEY` | ✅ | `memu-dev-key` | Change in production |
| `OPENAI_API_KEY` | Optional | — | For OpenAI embeddings / chat |
| `EMBEDDING_BASE_URL` | Optional | `""` | Ollama or OpenAI-compat base URL |
| `EMBEDDING_MODEL` | Optional | `BAAI/bge-small-en-v1.5` | fastembed model (local, no URL needed) |
| `EMBEDDING_DIMS` | Optional | `384` | Must match model dims |
| `NATS_RAILWAY_URL` | Optional | — | Event streaming; API works without it |
| `NATS_LOCAL_URL` | Optional | — | Only for local dev |
| `TEMPORAL_HOST` | Optional | — | Required for async workflow routes only |
| `LOG_LEVEL` | Optional | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `DEDUP_THRESHOLD` | Optional | `0.95` | Cosine similarity for dedup |

## Health Contract
- **Endpoint:** `GET /health`
- **Healthy response:** `200 {"status":"healthy","version":"0.1.0"}`
- **Unhealthy:** `503` — means DB connection is down
- Railway healthcheck uses `/health` with 30s timeout (see `railway.toml`)

## Notes
- NATS is optional — API starts and operates fully without it (events are silently skipped)
- Temporal is optional — async routes return 503 if `TEMPORAL_HOST` is not set
- fastembed uses `BAAI/bge-small-en-v1.5` (384-dim) by default — no external embedding service needed
- Migrations run automatically on startup via `memu/migrations/`
