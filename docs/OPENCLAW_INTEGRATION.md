# OpenClaw Integration Guide

fumemory is the **Memory Evidence Plane** for OpenClaw. OpenClaw remains the top-level coordinator for task routing, gateway selection, agent orchestration, and completion decisions. fumemory receives canonical evidence writes, returns learning-oriented recall by default, and exposes deployment verification gates that prove Railway-backed services are usable.

## Canonical write path

OpenClaw gateways must use the synchronous canonical write endpoint for evidence:

```bash
POST /api/v1/memu/add
X-MemU-Key: <MEMU_API_KEY>
Content-Type: application/json

{
  "content": "Gateway gw-railway-1 completed deployment smoke for task deploy-2026-05-04",
  "memory_type": "fact",
  "memory_kind": "evidence",
  "agent_id": "gw-railway-1",
  "idempotency_key": "deploy-smoke-gw-railway-1-2026-05-04",
  "metadata": {
    "task_id": "deploy-2026-05-04",
    "session_id": "ses-gw-001",
    "gateway_id": "gw-railway-1",
    "event_type": "deployment_smoke",
    "criticality": "completion_proof"
  }
}
```

The response includes a durable `id`. Evidence is immediately searchable before the gateway proceeds.

### Evidence criticality

| Value | Behavior |
|-------|----------|
| `completion_proof` | Write failure blocks task completion, review approval, or gateway readiness. The gateway must surface the error to OpenClaw and wait for proof or a human/operator waiver. |
| `telemetry` | Write failure is retried or queued without blocking completion. Failure is recorded in local logs; it must not silently pretend the write succeeded. |

Unknown criticality resolves to `completion_proof` when attached to a completion/review/federation event, and `telemetry` only for explicitly low-risk activity logs.

### Idempotency

Evidence writes are idempotent by `(tenant_id, idempotency_key)`:

- Exact replay (same key, same canonical payload hash) returns the original memory ID.
- Same key with a different canonical payload hash returns `409 Conflict`.
- Two distinct events with the same content but different task/session/gateway IDs remain separate evidence records.

### Hook contract

OpenClaw hooks must write through the synchronous canonical path, not `/memories/async`. Hook failures must be visible to the caller when the write is `completion_proof`.

```python
# Correct: synchronous canonical write
import httpx

def write_completion_evidence(content: str, task_id: str, session_id: str, gateway_id: str, idempotency_key: str) -> str:
    resp = httpx.post(
        f"{MEMU_API_URL}/api/v1/memu/add",
        headers={"X-MemU-Key": MEMU_API_KEY},
        json={
            "content": content,
            "memory_type": "fact",
            "memory_kind": "evidence",
            "agent_id": gateway_id,
            "idempotency_key": idempotency_key,
            "metadata": {
                "task_id": task_id,
                "session_id": session_id,
                "gateway_id": gateway_id,
                "criticality": "completion_proof",
            },
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]
```

`/memories/async` remains optional until it accepts the same schema fields, preserves idempotency keys, and exposes equivalent validation errors.

---

## Default learning recall

Before executing a task, inject Learning Memory into OpenClaw context:

```bash
GET /api/v1/recall?query=embedding+configuration+env+var&limit=5
X-MemU-Key: <MEMU_API_KEY>
```

Default recall returns only `accepted`, `accepted_by_timeout`, and `legacy` Learning Memory. Raw Evidence Memory never appears in default recall.

```python
import httpx

def recall_learning(query: str, limit: int = 5) -> list[dict]:
    resp = httpx.get(
        f"{MEMU_API_URL}/api/v1/recall",
        headers={"X-MemU-Key": MEMU_API_KEY},
        params={"query": query, "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["results"]
```

---

## Forensic Recall

Use Forensic Recall when you need proof, replay, debugging, or task audit — not for normal agent context.

```bash
POST /api/v1/recall/forensic
X-MemU-Key: <MEMU_API_KEY>
Content-Type: application/json

{
  "task_id": "deploy-2026-05-04",
  "session_id": "ses-gw-001",
  "gateway_id": "gw-railway-1",
  "event_type": "deployment_smoke",
  "limit": 20
}
```

Forensic Recall returns Evidence Memory records with task/session/gateway provenance. Content may be redacted for tenant, role, or safety reasons, but the response always includes the evidence ID, event type, actor, timestamp, source ref, artifact refs, and redaction reason when content is withheld.

Supported filters: `task_id`, `session_id`, `gateway_id`, `agent_id`, `event_type`, `time_window_start`, `time_window_end`, `artifact_ref`, `limit`, `cursor`, `include_content`.

---

## Telegram reflection review

After meaningful task completion, the Reflection Worker distills Evidence Memory into proposed Learning Memory and delivers a **Compact Reflection Notice** to Telegram via OpenClaw.

### Compact Reflection Notice format

Telegram messages are compact by default. Each notice includes:

- Proposed learning summary (no raw forensic evidence)
- Confidence score and risk flags
- Source task ID and session ID
- Expiry timestamp (six hours from delivery)
- Actions: **Approve**, **Deny**, **Edit**, **Inspect evidence**

The **Inspect evidence** action fetches full source evidence through Forensic Recall — it does not dump raw proof into the Telegram thread.

### Review lifecycle

| Status | Description |
|--------|-------------|
| `proposed` | Initial state; proposal is in the Reflection Review Queue |
| `accepted` | User explicitly approved the learning |
| `accepted_by_timeout` | No user action within six hours; auto-integrated |
| `rejected` | User denied the learning; excluded from default recall |

Approved and timeout-integrated Learning Memory is eligible for default recall. Rejected proposals never enter default recall. Late feedback after auto-integration creates a superseding Learning Memory record rather than mutating the integrated memory.

### Review state owner

Telegram is the **notification and action surface**, not the canonical state store. All actions (approve, deny, edit, inspect) are persisted through the Reflection Review Queue in fumemory before any proposal state changes. If Telegram delivery fails, the proposal remains pending in the queue and still follows the six-hour timeout policy.

### API endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/reflection/proposals` | POST | Create a reflection proposal |
| `/api/v1/reflection/proposals` | GET | List proposals (filter by status, source) |
| `/api/v1/reflection/proposals/{id}` | GET | Get a single proposal |
| `/api/v1/reflection/proposals/{id}/action` | POST | Approve, deny, or edit a proposal |
| `/api/v1/reflection/timeouts` | POST | Process expired proposals (run on schedule) |

### Example: approve a proposal

```bash
POST /api/v1/reflection/proposals/<proposal-id>/action
X-MemU-Key: <MEMU_API_KEY>
Content-Type: application/json

{
  "actor": "operator-1",
  "decision": "approve"
}
```

### Reflection cadence

- **Task-close reflection** runs after meaningful task completion and emits at most 1–3 proposed Learning Memories from that task's evidence.
- **Idle/dream reflection** runs on a schedule and looks for cross-task patterns, repeated failures, and stale learning candidates.
- **High-risk or high-value** proposals notify immediately via Telegram; routine proposals batch into digests delivered every 2–4 hours.
- Each digest item keeps its own six-hour review window from when it is delivered or made visible in the review queue.

---

## Federation Readiness for gateways

A gateway must pass **Federation Readiness** before it is considered available for shared swarm work.

```bash
# Prove Core API + NATS + idempotency + searchable memory
python scripts/verify_deployment.py \
  --api-url https://<fumemory-api>.up.railway.app \
  --check-federation \
  --proof-out proof-federation.json
```

The proof artifact (`proof-federation.json`) includes evidence memory IDs, check statuses, and gateway ID — with all secrets redacted. fumemory emits the proof; OpenClaw or an operator decides whether to mark the gateway swarm-ready.

---

## Environment variables

| Variable | Required | Notes |
|----------|----------|-------|
| `MEMU_API_URL` | Yes | Base URL for the fumemory API |
| `MEMU_API_KEY` | Yes | Auth key (`X-MemU-Key` header) |
| `EMBEDDING_API_BASE` | Recommended | Canonical embedding provider base URL |
| `EMBEDDING_MODEL` | Recommended | Embedding model name (e.g. `text-embedding-3-small`) |
| `EMBEDDING_DIMS` | Recommended | Must match active vector schema (e.g. `1536`) |
| `NATS_RAILWAY_URL` | Federation only | Required for Federation Readiness gate |
| `GATEWAY_ID` | Federation only | Included in federation proof artifacts |
| `TEMPORAL_HOST` | Async only | Required for `/memories/async` and `/search/async` |

`EMBEDDING_BASE_URL` is a deprecated compatibility alias for `EMBEDDING_API_BASE`. Use `EMBEDDING_API_BASE` in all new configuration.
