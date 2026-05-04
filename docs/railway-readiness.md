# Railway readiness checklist

This repo can run several different service shapes on Railway. Do not treat them as one deploy.

## Service matrix

### Core API service
Required to boot cleanly:
- `DATABASE_URL`
- `MEMU_API_KEY`

Optional at boot:
- `NATS_RAILWAY_URL` — enables mesh publish/subscribe and cluster status endpoints
- `EMBEDDING_API_BASE` / `EMBEDDING_MODEL` / `EMBEDDING_DIMS` — enables semantic embeddings. `EMBEDDING_BASE_URL` is a deprecated alias for `EMBEDDING_API_BASE`.
- `OPENAI_API_KEY` — required for OpenAI-hosted embeddings and chat features

Behavior notes:
- API startup is DB-blocking and NATS-nonblocking.
- If NATS is missing, API should still serve CRUD/search endpoints.
- Railway injects `PORT`; `memu.api` already binds to it.

### NATS service
Required:
- Railway TCP port mapping must be honored by the container entrypoint.

Behavior notes:
- Use `infra/railway-nats/start-nats.sh` as the start command.
- Do not override it with a conflicting `railway.json` inline command.
- JetStream storage path and port selection must stay consistent across Dockerfile + Railway config.

### Temporal service / worker
Required only for async endpoints:
- `TEMPORAL_HOST`
- `TEMPORAL_TLS` when applicable
- a healthy Temporal server
- a healthy `memu.temporal_worker.worker` process listening on `memu-queue`

Behavior notes:
- `/memories/async` and `/search/async` should be considered optional features until Temporal is green.
- Do not fail core API verification because Temporal is absent.

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
   - Full async deploy: run `python scripts/verify_deployment.py --api-url <url> --check-async`.
   - If `--check-async` fails, fix Temporal or skip async claims from release notes.

## Recommended pre-deploy smoke

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
pytest -q tests/test_gateway_leases_api.py tests/test_search_upgrade.py tests/test_memu_auto_write_nats_fallback.py
python scripts/verify_deployment.py --api-url http://127.0.0.1:8000
# add --check-async only when Temporal is deployed too
```

## Known non-blockers

- Missing NATS should degrade mesh features, not crash API boot.
- Missing embedding provider should degrade semantic search quality, not crash API boot.
- Missing Temporal is acceptable only if async endpoints are not part of the release contract.
