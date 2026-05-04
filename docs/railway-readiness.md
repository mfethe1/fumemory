# Railway readiness checklist

This repo deploys as five named services on Railway. Do not treat them as one deploy.

## Railway topology

| Service name         | Required | Role |
|----------------------|----------|------|
| `api`                | yes      | fumemory FastAPI process; canonical write, recall, and auth |
| `postgres-pgvector`  | yes      | Postgres with pgvector extension; persists all memories |
| `nats-jetstream`     | no (federation) | NATS server with JetStream; enables gateway mesh publish/consume |
| `temporal-worker`    | no (async)      | Temporal workflow worker; required for `/memories/async` and `/search/async` |
| `embedding-service`  | no (optional)   | Hosted embedding endpoint (e.g. Ollama); improves semantic search quality |

**Core API Readiness** requires only `api` and `postgres-pgvector`.
**Federation Readiness** additionally requires `nats-jetstream`.
**Async Readiness** additionally requires `temporal-worker`.

## Readiness gates

### Core API Readiness

Proves the fumemory API is functional without NATS or Temporal:

- `GET /health` returns 200 with DB probe passing
- `POST /memories` performs a canonical write with auth
- `GET /search-text?q=...` retrieves the written memory (immediate recall)
- `GET /search/recall?query=...` retrieves the written memory (semantic recall)

Run with:
```bash
python scripts/verify_deployment.py --api-url <url>
```

Emit a machine-readable proof artifact:
```bash
python scripts/verify_deployment.py --api-url <url> --proof-out proof-core.json
```

### Federation Readiness

Requires Core API Readiness plus:

- Idempotency-keyed evidence write (proves canonical write path with dedup)
- Idempotency replay: second write with the same key returns the existing memory (proves dedup)
- Searchable memory proof: the idempotency-keyed memory is findable via `/search/recall`

NATS/JetStream gateway publish/consume and directed response are proved by the test suite
(`tests/test_gateway_federation.py`, `tests/test_nats_config_contract.py`) running against a
live or containerized NATS service. The HTTP verify script proves the memory-plane half of
Federation Readiness (idempotency, searchability, auth).

Run with:
```bash
python scripts/verify_deployment.py --api-url <url> --check-federation
```

Emit a machine-readable federation proof:
```bash
python scripts/verify_deployment.py --api-url <url> --check-federation --proof-out proof-federation.json
```

### Async Readiness

Requires Core API Readiness plus a healthy Temporal server and `temporal-worker`:

- `POST /memories/async` accepts the request
- `POST /search/async` accepts the request

Run only when Temporal is explicitly deployed:
```bash
python scripts/verify_deployment.py --api-url <url> --check-async
```

Optional services (`nats-jetstream`, `temporal-worker`, `embedding-service`) must **not** block
Core API Readiness. The API starts successfully and serves CRUD/search endpoints even when they
are absent.

## Service matrix

### `api` service
Required env vars at boot:
- `DATABASE_URL`
- `MEMU_API_KEY`

Optional env vars:
- `NATS_RAILWAY_URL` — enables mesh publish/subscribe and cluster status endpoints
- `EMBEDDING_API_BASE` / `EMBEDDING_MODEL` / `EMBEDDING_DIMS` — enables semantic embeddings; `EMBEDDING_BASE_URL` is a deprecated alias for `EMBEDDING_API_BASE`
- `OPENAI_API_KEY` — required for OpenAI-hosted embeddings and chat features

Behavior notes:
- API startup is DB-blocking and NATS-nonblocking.
- If NATS is missing, API still serves CRUD/search endpoints.
- Railway injects `PORT`; `memu.api` already binds to it.

### `nats-jetstream` service
Required:
- Railway TCP port mapping must be honored by the container entrypoint.

Behavior notes:
- Use `infra/railway-nats/start-nats.sh` as the start command.
- Do not override it with a conflicting `railway.json` inline command.
- JetStream storage path and port selection must stay consistent across Dockerfile + Railway config.

### `temporal-worker` service
Required only for async endpoints:
- `TEMPORAL_HOST`
- `TEMPORAL_TLS` when applicable
- a healthy Temporal server
- a healthy `memu.temporal_worker.worker` process listening on `memu-queue`

Behavior notes:
- `/memories/async` and `/search/async` are optional features until Temporal is green.
- Do not fail core API verification because Temporal is absent.

### `embedding-service` (optional)
- Ollama or compatible OpenAI-style endpoint.
- Missing embedding provider degrades semantic search quality; it does not crash API boot.

## Exact blockers to clear before next Railway deploy attempt

1. **Package install must succeed from repo metadata**
   - `pip install -e .` must work in a clean venv.
   - This is required for repeatable local smoke and any future build path that installs from source metadata.

2. **Dockerfile dependency specs must be shell-safe**
   - Version constraints like `fastapi>=0.100` must be quoted or installed from a requirements file.
   - Unquoted `>` in `RUN pip install ...` is shell redirection, not a version constraint.

3. **NATS Railway config must use one startup contract**
   - `infra/railway-nats/railway.json` and `start-nats.sh` must agree on port + storage path.
   - Current deploy path should use `/start-nats.sh`.

4. **No committed hardcoded Railway endpoints or secrets in operational scripts**
   - Scripts must read `MEMU_API_URL`, `MEMU_API_KEY`, `NATS_RAILWAY_URL`, etc. from env.
   - Do not commit concrete `*.up.railway.app`, `*.proxy.rlwy.net`, or real API keys as defaults.

5. **Verification must match what is actually deployed**
   - Core deploy: run `python scripts/verify_deployment.py --api-url <url>`.
   - Federation deploy: run `python scripts/verify_deployment.py --api-url <url> --check-federation`.
   - Full async deploy: run `python scripts/verify_deployment.py --api-url <url> --check-async`.
   - If `--check-federation` fails, fix NATS or skip federation claims from release notes.
   - If `--check-async` fails, fix Temporal or skip async claims from release notes.

## Recommended pre-deploy smoke

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
pytest -q tests/test_gateway_leases_api.py tests/test_search_upgrade.py tests/test_memu_auto_write_nats_fallback.py
python scripts/verify_deployment.py --api-url http://127.0.0.1:8000
# add --check-federation only when nats-jetstream is deployed too
# add --check-async only when temporal-worker is deployed too
```

## Machine-readable proof artifacts

The `--proof-out FILE` flag writes a JSON proof document to FILE. All secrets (API key, auth
header values) are redacted as `"[REDACTED]"`. The document structure is:

```json
{
  "schema_version": 1,
  "gate": "core",
  "api_url": "http://127.0.0.1:8000",
  "api_key": "[REDACTED]",
  "timestamp_utc": "2026-05-04T17:00:00Z",
  "checks": [
    {"name": "health", "passed": true, "detail": "status=200"},
    {"name": "canonical_write", "passed": true, "detail": "memory_id written"},
    {"name": "search_text", "passed": true, "detail": "memory found"},
    {"name": "search_recall", "passed": true, "detail": "memory found"}
  ],
  "overall": "pass"
}
```

Federation proof adds `idempotency_write` and `idempotency_replay` checks.
Async proof adds `async_ingest` and `async_search` checks.

## Known non-blockers

- Missing NATS should degrade mesh features, not crash API boot.
- Missing embedding provider should degrade semantic search quality, not crash API boot.
- Missing Temporal is acceptable only if async endpoints are not part of the release contract.
