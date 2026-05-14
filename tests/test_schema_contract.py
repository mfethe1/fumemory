"""Tests for the Evidence Memory / Learning Memory schema contract (Issue #25).

Coverage:
- memory_kind distinguishes evidence and learning without replacing memory_type
- Evidence idempotency: exact replay returns same ID; mismatched replay → 409
- Evidence Memory is append-only and skips content-hash dedup
- Legacy backfill: deterministic classification rules
- Canonical payload hash: determinism and transport-field exclusion
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memu import api
from memu.models import MemoryCreate, MemoryKind, MemoryType
from memu.schema_contract import (
    EVIDENCE_MEMORY_TYPES,
    LEARNING_MEMORY_TYPES,
    OPENCLAW_EXECUTION_METADATA_KEYS,
    classify_legacy_row,
    compute_canonical_payload_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(
    id=None,
    content="some content",
    memory_type="observation",
    memory_kind="learning",
    review_status=None,
    canonical_payload_hash=None,
    idempotency_key=None,
    metadata=None,
):
    now = datetime.now(timezone.utc)
    return {
        "id": id or uuid4(),
        "content": content,
        "memory_type": memory_type,
        "memory_kind": memory_kind,
        "agent_id": "test-agent",
        "metadata": metadata or {},
        "parent_id": None,
        "confidence": 1.0,
        "access_count": 0,
        "decay_score": 1.0,
        "salience_score": 0.5,
        "searchable": True,
        "review_status": review_status,
        "canonical_payload_hash": canonical_payload_hash,
        "idempotency_key": idempotency_key,
        "created_at": now,
        "updated_at": now,
    }


def _fake_tenant_conn(conn):
    @asynccontextmanager
    async def _ctx(_auth):
        yield conn
    return _ctx


# ---------------------------------------------------------------------------
# 1. memory_kind field presence and defaults
# ---------------------------------------------------------------------------

def test_memory_create_defaults_memory_kind_to_learning():
    req = MemoryCreate(content="hello", agent_id="agent-1")
    assert req.memory_kind == MemoryKind.learning


def test_memory_create_accepts_evidence_kind():
    req = MemoryCreate(content="hello", agent_id="agent-1", memory_kind=MemoryKind.evidence)
    assert req.memory_kind == MemoryKind.evidence


def test_memory_kind_does_not_replace_memory_type():
    """memory_kind and memory_type are independent fields."""
    req = MemoryCreate(
        content="tool call output",
        agent_id="agent-1",
        memory_kind=MemoryKind.evidence,
        memory_type=MemoryType.user_action,
    )
    assert req.memory_kind == MemoryKind.evidence
    assert req.memory_type == MemoryType.user_action


# ---------------------------------------------------------------------------
# 2. Idempotency: exact replay returns same ID
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exact_replay_returns_same_id(monkeypatch):
    existing_id = uuid4()
    canonical_hash = compute_canonical_payload_hash(
        content="gateway started",
        memory_type="user_action",
        agent_id="gw-1",
        metadata={"task_id": "task-abc"},
    )
    existing_row = _make_row(
        id=existing_id,
        content="gateway started",
        memory_type="user_action",
        memory_kind="evidence",
        canonical_payload_hash=canonical_hash,
        idempotency_key="idem-001",
        metadata={"task_id": "task-abc"},
    )

    conn = AsyncMock()
    # First fetchrow: idempotency lookup returns existing row
    # Second fetchrow: SELECT * returns the full existing row
    conn.fetchrow.side_effect = [
        {"id": existing_id, "canonical_payload_hash": canonical_hash},
        existing_row,
    ]

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_nats_publisher", None)
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await api.create_memory(
        MemoryCreate(
            content="gateway started",
            memory_type=MemoryType.user_action,
            memory_kind=MemoryKind.evidence,
            agent_id="gw-1",
            metadata={"task_id": "task-abc"},
            idempotency_key="idem-001",
        ),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.id == existing_id


# ---------------------------------------------------------------------------
# 3. Idempotency: mismatched payload returns 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mismatched_replay_raises_409(monkeypatch):
    from fastapi import HTTPException

    existing_id = uuid4()
    stored_hash = "aaa" + "0" * 61  # 64 chars, different from real hash

    conn = AsyncMock()
    # Idempotency lookup returns a row with a DIFFERENT hash
    conn.fetchrow.return_value = {"id": existing_id, "canonical_payload_hash": stored_hash}

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_nats_publisher", None)
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    with pytest.raises(HTTPException) as exc_info:
        await api.create_memory(
            MemoryCreate(
                content="different content for same key",
                memory_type=MemoryType.user_action,
                memory_kind=MemoryKind.evidence,
                agent_id="gw-1",
                metadata={"task_id": "task-abc"},
                idempotency_key="idem-001",
            ),
            _key=api.AuthContext("memu-dev-key"),
        )

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["error"] == "idempotency_conflict"
    assert detail["existing_id"] == str(existing_id)


# ---------------------------------------------------------------------------
# 4. Evidence Memory skips content-hash dedup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evidence_memory_inserts_without_content_dedup(monkeypatch):
    """Two distinct evidence writes with identical content must remain separate records."""
    inserted_id = uuid4()
    datetime.now(timezone.utc)
    inserted_row = _make_row(
        id=inserted_id,
        content="task completed",
        memory_type="user_action",
        memory_kind="evidence",
    )

    conn = AsyncMock()
    # No idempotency_key — idempotency check is skipped entirely.
    # fetchrow is only called for the INSERT RETURNING.
    conn.fetchrow.return_value = inserted_row

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_nats_publisher", None)
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await api.create_memory(
        MemoryCreate(
            content="task completed",
            memory_type=MemoryType.user_action,
            memory_kind=MemoryKind.evidence,
            agent_id="gw-1",
        ),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.id == inserted_id
    # Verify INSERT was called (not UPDATE — no dedup hit)
    insert_call = conn.fetchrow.await_args_list[0]
    assert "INSERT INTO memories" in insert_call.args[0]
    assert "memory_kind" in insert_call.args[0]


# ---------------------------------------------------------------------------
# 5. Learning Memory still uses content-hash dedup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_learning_memory_deduplicates_on_content_hash(monkeypatch):
    """Learning Memory with identical content should hit the dedup path."""
    existing_id = uuid4()
    datetime.now(timezone.utc)
    updated_row = _make_row(id=existing_id, memory_kind="learning")

    conn = AsyncMock()
    # fetchrow returns an existing row with similarity=1.0 (exact hash match)
    conn.fetchrow.side_effect = [
        {"id": existing_id, "similarity": 1.0},  # dedup check
        updated_row,                               # UPDATE RETURNING
    ]

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_nats_publisher", None)
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await api.create_memory(
        MemoryCreate(
            content="some repeated lesson",
            memory_type=MemoryType.lesson,
            memory_kind=MemoryKind.learning,
            agent_id="agent-1",
        ),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.id == existing_id
    update_call = conn.fetchrow.await_args_list[1]
    assert "UPDATE memories" in update_call.args[0]


# ---------------------------------------------------------------------------
# 6. Evidence Memory INSERT includes memory_kind column
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evidence_insert_sets_memory_kind_column(monkeypatch):
    inserted_id = uuid4()
    inserted_row = _make_row(
        id=inserted_id,
        memory_kind="evidence",
        memory_type="user_action",
    )

    conn = AsyncMock()
    conn.fetchrow.return_value = inserted_row

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_nats_publisher", None)
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await api.create_memory(
        MemoryCreate(
            content="execution event",
            memory_type=MemoryType.user_action,
            memory_kind=MemoryKind.evidence,
            agent_id="gw-1",
        ),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.memory_kind == "evidence"
    insert_call = conn.fetchrow.await_args_list[0]
    # The INSERT SQL should include memory_kind
    assert "memory_kind" in insert_call.args[0]
    # The value passed should be "evidence"
    assert "evidence" in insert_call.args


# ---------------------------------------------------------------------------
# 7. Legacy backfill: learning types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("memory_type", sorted(LEARNING_MEMORY_TYPES))
def test_classify_learning_types_return_learning_legacy(memory_type):
    kind, status = classify_legacy_row(memory_type)
    assert kind == "learning"
    assert status == "legacy"


# ---------------------------------------------------------------------------
# 8. Legacy backfill: evidence types
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("memory_type", sorted(EVIDENCE_MEMORY_TYPES))
def test_classify_evidence_types_return_evidence(memory_type):
    kind, status = classify_legacy_row(memory_type)
    assert kind == "evidence"
    assert status is None


# ---------------------------------------------------------------------------
# 9. Legacy backfill: observation with OpenClaw execution metadata → evidence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("openclaw_key", sorted(OPENCLAW_EXECUTION_METADATA_KEYS))
def test_classify_observation_with_openclaw_metadata_is_evidence(openclaw_key):
    kind, status = classify_legacy_row("observation", metadata={openclaw_key: "some-value"})
    assert kind == "evidence"
    assert status is None


# ---------------------------------------------------------------------------
# 10. Legacy backfill: observation without OpenClaw execution metadata → learning
# ---------------------------------------------------------------------------

def test_classify_observation_without_openclaw_metadata_is_learning():
    kind, status = classify_legacy_row("observation", metadata={"note": "generic note"})
    assert kind == "learning"
    assert status == "legacy"


def test_classify_observation_no_metadata_is_learning():
    kind, status = classify_legacy_row("observation")
    assert kind == "learning"
    assert status == "legacy"


# ---------------------------------------------------------------------------
# 11. Legacy backfill: unknown type defaults to learning/legacy
# ---------------------------------------------------------------------------

def test_classify_unknown_type_defaults_to_learning_legacy():
    kind, status = classify_legacy_row("totally_new_type")
    assert kind == "learning"
    assert status == "legacy"


# ---------------------------------------------------------------------------
# 12. Canonical payload hash: determinism
# ---------------------------------------------------------------------------

def test_canonical_payload_hash_is_deterministic():
    h1 = compute_canonical_payload_hash(
        content="task finished",
        memory_type="user_action",
        agent_id="gw-1",
        metadata={"task_id": "abc", "session_id": "ses-1"},
    )
    h2 = compute_canonical_payload_hash(
        content="task finished",
        memory_type="user_action",
        agent_id="gw-1",
        metadata={"task_id": "abc", "session_id": "ses-1"},
    )
    assert h1 == h2
    assert len(h1) == 64


# ---------------------------------------------------------------------------
# 13. Canonical payload hash: transport fields are excluded
# ---------------------------------------------------------------------------

def test_canonical_payload_hash_excludes_transport_fields():
    """Retries with different ts/ingested_at must produce the same hash."""
    h1 = compute_canonical_payload_hash(
        content="tool output",
        memory_type="user_action",
        agent_id="gw-1",
        metadata={"task_id": "t1", "ts": "2026-05-01T10:00:00Z"},
    )
    h2 = compute_canonical_payload_hash(
        content="tool output",
        memory_type="user_action",
        agent_id="gw-1",
        metadata={"task_id": "t1", "ts": "2026-05-04T22:00:00Z"},
    )
    assert h1 == h2


# ---------------------------------------------------------------------------
# 14. Canonical payload hash: different content produces different hash
# ---------------------------------------------------------------------------

def test_canonical_payload_hash_differs_for_different_content():
    h1 = compute_canonical_payload_hash("content A", "user_action", "gw-1")
    h2 = compute_canonical_payload_hash("content B", "user_action", "gw-1")
    assert h1 != h2


# ---------------------------------------------------------------------------
# 15. Memory model includes memory_kind and review_status fields
# ---------------------------------------------------------------------------

def test_row_to_memory_includes_memory_kind_and_review_status():
    row = _make_row(memory_kind="evidence", review_status=None)
    mem = api._row_to_memory(row)
    assert mem.memory_kind == "evidence"
    assert mem.review_status is None


def test_row_to_memory_defaults_memory_kind_to_learning_when_missing():
    """Rows from before migration 020 have no memory_kind column — default gracefully."""
    now = datetime.now(timezone.utc)
    row = {
        "id": uuid4(),
        "content": "old memory",
        "memory_type": "observation",
        "agent_id": "legacy",
        "metadata": {},
        "parent_id": None,
        "confidence": 1.0,
        "access_count": 0,
        "decay_score": 1.0,
        "created_at": now,
        "updated_at": now,
        # memory_kind and review_status intentionally absent
    }
    mem = api._row_to_memory(row)
    assert mem.memory_kind == "learning"
    assert mem.review_status is None
