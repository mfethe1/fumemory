"""Schema contract for Evidence Memory and Learning Memory.

Defines canonical payload hash computation and deterministic legacy
backfill classification rules (memory_kind and review_status assignment).

Classification rules from PRD §Implementation Decisions:
  - lesson, decision, pattern, procedural, fact, reflection, plan, goal
      → learning / review_status=legacy
  - user_action, external, failure
      → evidence / review_status=None
  - observation WITH OpenClaw execution metadata (task_id/session_id/gateway_id/event_type)
      → evidence / review_status=None
  - observation WITHOUT OpenClaw execution metadata
      → learning / review_status=legacy
  - Unknown types
      → learning / review_status=legacy  (conservative default)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


# ---------------------------------------------------------------------------
# Classification sets
# ---------------------------------------------------------------------------

# Memory types that are deterministically Learning Memory (reusable knowledge)
LEARNING_MEMORY_TYPES: frozenset[str] = frozenset({
    "lesson",
    "decision",
    "pattern",
    "procedural",
    "fact",
    "reflection",
    "plan",
    "goal",
})

# Memory types that are deterministically Evidence Memory (execution proof)
EVIDENCE_MEMORY_TYPES: frozenset[str] = frozenset({
    "user_action",
    "external",
    "failure",
})

# Memory types whose classification depends on their metadata
UNCERTAIN_MEMORY_TYPES: frozenset[str] = frozenset({"observation"})

# Presence of any of these metadata keys marks an uncertain row as evidence
OPENCLAW_EXECUTION_METADATA_KEYS: frozenset[str] = frozenset({
    "task_id",
    "session_id",
    "gateway_id",
    "event_type",
})

# ---------------------------------------------------------------------------
# Canonical payload hash
# ---------------------------------------------------------------------------

# Fields included in the canonical hash for evidence idempotency.
# Order is stable so serialization is deterministic.
_CANONICAL_FIELDS = (
    "content",
    "memory_type",
    "agent_id",
    "task_id",
    "session_id",
    "gateway_id",
    "event_type",
    "event_at",
    "source",
    "source_ref",
)

# Transport-only metadata keys excluded from the hash.  They differ between
# retries (e.g. current timestamp) but must not invalidate idempotent replays.
TRANSPORT_ONLY_METADATA_KEYS: frozenset[str] = frozenset({
    "ts",
    "ingested_at",
    "allowed_roles",
    "entities",
})


def compute_canonical_payload_hash(
    content: str,
    memory_type: str,
    agent_id: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Return a 64-hex-char SHA-256 hash of the canonical evidence payload.

    Only canonical evidence fields are included.  Transport-only metadata
    fields (ts, ingested_at, allowed_roles) are excluded so that retries
    with identical evidence content produce the same hash regardless of
    when they are submitted.

    The hash is used solely for idempotency validation:
    - same (tenant_id, idempotency_key) + same hash  → exact replay, return existing ID
    - same (tenant_id, idempotency_key) + diff hash  → 409 Conflict
    """
    m = metadata or {}
    canonical: dict[str, Any] = {
        "content": (content or "").strip(),
        "memory_type": memory_type or "",
        "agent_id": agent_id or "",
    }
    for field in ("task_id", "session_id", "gateway_id", "event_type", "event_at", "source", "source_ref"):
        val = m.get(field)
        if val is not None:
            canonical[field] = val
    serialized = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(serialized.encode()).hexdigest()[:64]


# ---------------------------------------------------------------------------
# Legacy backfill classification
# ---------------------------------------------------------------------------

def classify_legacy_row(
    memory_type: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Classify a legacy memory row into evidence or learning.

    Returns (memory_kind, review_status) where memory_kind is one of
    'evidence' / 'learning' and review_status is None (evidence) or
    'legacy' (learning rows from before the schema contract existed).

    Deterministic rules (from PRD §Implementation Decisions):
    - lesson/decision/pattern/procedural/fact/reflection/plan/goal → ('learning', 'legacy')
    - user_action/external/failure → ('evidence', None)
    - observation WITH OpenClaw execution metadata → ('evidence', None)
    - observation WITHOUT OpenClaw execution metadata → ('learning', 'legacy')
    - unknown types → ('learning', 'legacy')  [conservative]
    """
    if memory_type in LEARNING_MEMORY_TYPES:
        return "learning", "legacy"

    if memory_type in EVIDENCE_MEMORY_TYPES:
        return "evidence", None

    if memory_type in UNCERTAIN_MEMORY_TYPES:
        m = metadata or {}
        has_openclaw_metadata = any(k in m for k in OPENCLAW_EXECUTION_METADATA_KEYS)
        if has_openclaw_metadata:
            return "evidence", None
        return "learning", "legacy"

    # Unknown type — default to learning/legacy so it can be reviewed
    return "learning", "legacy"
