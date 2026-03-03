# Cryptographic Compliance Engine — Implementation Blueprint

**Status:** PLANNED
**Author:** Michael Fethe (design), Lenny (implementation plan)
**Date:** 2026-03-03

## Overview
Transform fumemory from orchestration middleware into an underwriter-ready compliance engine ("Black-Box Flight Recorder for AI") with cryptographic provability.

## Existing Foundation
- ✅ Append-only `events` table (events_schema.sql)
- ✅ `signature` column on SwarmEvent (placeholder)
- ✅ Bi-temporal columns (`valid_from`, `valid_to`) on memories
- ✅ Dead Letter Queue with failure tracking
- ✅ Glass Box UI for DAG visualization
- ✅ Lane locks with fencing tokens

---

## Step 1: Cryptographic Enforcing (Immutable Ledger)

### 1.1 Ed25519 Key Generation in boot.py
```python
# File: memu/crypto.py (NEW)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

def generate_gateway_keypair() -> tuple[bytes, bytes]:
    """Generate ephemeral Ed25519 keypair for gateway signing."""
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw
    )
    private_bytes = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption()
    )
    return private_bytes, public_bytes

def sign_event(private_key_bytes: bytes, payload: bytes) -> bytes:
    """Sign event payload with Ed25519."""
    key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return key.sign(payload)

def verify_signature(public_key_bytes: bytes, payload: bytes, signature: bytes) -> bool:
    """Verify Ed25519 signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        key.verify(signature, payload)
        return True
    except Exception:
        return False
```

### 1.2 Sign Events in nats_publisher.py
- Before publishing any SwarmEvent, create deterministic hash of `payload + timestamp + task_id`
- Sign with gateway's ephemeral private key
- Populate the `signature` field with hex-encoded Ed25519 signature

### 1.3 Register Public Keys in gateway_registry
- On boot, register public key in `gateway_registry.metadata.public_key`
- On shutdown, mark key as revoked

### 1.4 Daily Merkle Root Anchor (cron job)
- Collect all `event_id`s from past 24h
- Build Merkle tree
- Publish root to S3 Object Lock bucket (WORM storage)
- Store root hash in `events` table as a `merkle_anchor` event type

### Schema Changes
```sql
-- Add to events_schema.sql
ALTER TABLE events ALTER COLUMN signature TYPE VARCHAR(512);
ALTER TABLE gateway_registry ADD COLUMN IF NOT EXISTS public_key BYTEA;
ALTER TABLE gateway_registry ADD COLUMN IF NOT EXISTS key_registered_at TIMESTAMPTZ;

-- New event type
-- Add 'merkle_anchor' to event_type CHECK constraint
```

### Dependencies
- `cryptography` Python package (Ed25519)
- AWS S3 with Object Lock (or Cloudflare R2 equivalent)

---

## Step 2: Proof of Context (State Snapshots)

### 2.1 Context Snapshot on Decisions
Update `DecisionMade` and `ExecutionResult` models in swarm_models.py:

```python
class ContextSnapshot(BaseModel):
    """Exact memory IDs and version hashes that were in the LLM prompt window."""
    memory_ids: list[UUID]
    memory_version_hashes: list[str]  # SHA256 of content at valid_from timestamp
    blackboard_entries: list[UUID]
    prompt_template_hash: str
    model_id: str
    temperature: float
    timestamp: datetime

class DecisionMade(BaseModel):
    # ... existing fields ...
    context_snapshot: ContextSnapshot | None = None

class ExecutionResult(BaseModel):
    # ... existing fields ...
    context_snapshot: ContextSnapshot | None = None
```

### 2.2 Time Machine Endpoint
```
GET /api/forensics/playback/{task_id}
```
- Reads task's exact timestamp
- Queries bi-temporal memU at that timestamp (`valid_from <= ts AND (valid_to IS NULL OR valid_to > ts)`)
- Reconstructs agent's context window
- Returns: task DAG, memory state, blackboard state, decisions made

### 2.3 Memory Version Hashing
- On every memory write, compute SHA256 of `content + agent_id + timestamp`
- Store in `memories.version_hash` column
- This creates an immutable audit trail of what each memory version contained

---

## Step 3: Incident Report Compiler (Forensic Export)

### 3.1 New Module: forensics_engine.py
Triggered by:
- God Mode halt (`/api/halt`)
- DLQ entry (3+ failures)
- Circuit breaker activation
- Manual trigger (`/api/forensics/export/{task_id}`)

### 3.2 Incident Bundle Contents
```json
{
  "incident_id": "uuid",
  "timestamp": "ISO-8601",
  "severity": "critical|high|medium",
  "root_prompt": { "id": "uuid", "content": "original human intent" },
  "task_dag": { "nodes": [...], "edges": [...] },
  "gateway_signatures": [
    { "gateway_id": "...", "public_key": "...", "events_signed": 42 }
  ],
  "failure_chain": [
    { "event_id": "...", "type": "task_failed", "error": "...", "timestamp": "..." }
  ],
  "context_at_failure": {
    "memories": [...],
    "blackboard": [...],
    "lane_locks": [...]
  },
  "system_state": {
    "active_containers": 3,
    "cpu_usage": 0.45,
    "memory_mb": 2048
  },
  "merkle_proof": {
    "root": "...",
    "path": [...]
  }
}
```

### 3.3 Export Formats
- JSON (machine-readable)
- JSON-LD (semantic web / legal interop)
- PDF (human-readable, for compliance officers)

---

## Step 4: Actuarial DLQ Pipeline

### 4.1 Failure Taxonomy
Update DLQEntry in swarm_models.py:
```python
class FailureCategory(str, Enum):
    HALLUCINATION = "hallucination"
    API_TIMEOUT = "api_timeout"
    LOGIC_LOOP = "logic_loop"
    UNAUTHORIZED_DATA_ACCESS = "unauthorized_data_access"
    GUARDRAIL_VIOLATION = "guardrail_violation"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    DEPENDENCY_FAILURE = "dependency_failure"
    UNKNOWN = "unknown"

class DLQEntry(BaseModel):
    # ... existing fields ...
    failure_category: FailureCategory = FailureCategory.UNKNOWN
    failure_subcategory: str | None = None
```

### 4.2 Anonymized Aggregation
- Strip PII from failure data
- Aggregate by: failure_category, model_id, capability_type, time_window
- Build running statistics: MTBF, failure rate by category, recovery time

### 4.3 Actuarial API
```
GET /api/actuarial/summary?window=30d
GET /api/actuarial/failure-rates?category=hallucination
GET /api/actuarial/risk-score/{client_id}
```

---

## Sprint Plan

### Sprint 1 (This Week): Railway Fix + Foundation
- [x] Fix Railway log bloat (embedding dims, missing tables, log levels)
- [ ] Add `cryptography` to requirements.txt
- [ ] Create `memu/crypto.py` with Ed25519 key gen/sign/verify
- [ ] Add `version_hash` column to memories table
- [ ] Add `context_snapshot` to DecisionMade and ExecutionResult models

### Sprint 2: Signing Pipeline
- [ ] Wire crypto signing into nats_publisher.py
- [ ] Register public keys in gateway_registry on boot
- [ ] Add signature verification to event consumer
- [ ] Build Merkle tree cron job (daily anchor)

### Sprint 3: Forensics Engine
- [ ] Build `/api/forensics/playback/{task_id}` endpoint
- [ ] Build `forensics_engine.py` incident compiler
- [ ] JSON + JSON-LD export
- [ ] Wire DLQ → automatic incident report generation

### Sprint 4: Actuarial Pipeline
- [ ] Add FailureCategory enum and DLQ taxonomy
- [ ] Build anonymization pipeline
- [ ] Build actuarial summary endpoints
- [ ] Dashboard integration in Glass Box UI
