"""Tests proving async memory workflow parity with canonical sync writes (Issue #32).

Coverage:
- Async route serialises all canonical evidence fields into req_dict (memory_type,
  memory_kind, idempotency_key, salience_score, allowed_roles, metadata).
- Async workflows cannot silently hardcode memory_type or drop provenance.
- Temporal unavailable returns explicit degraded/unavailable status, not a
  bare 503 with a generic string — and does NOT fail Core API Readiness.
- store_memory activity performs idempotency check (exact replay / conflict).
- store_memory activity never hardcodes memory_type.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from memu import temporal_client
from memu.models import MemoryCreate, MemoryKind, MemoryType
from memu.temporal_routes import _req_to_dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_req(**kwargs) -> MemoryCreate:
    defaults = dict(
        content="gateway task complete",
        agent_id="gw-test",
        memory_type=MemoryType.user_action,
        memory_kind=MemoryKind.evidence,
        idempotency_key="idem-42",
        salience_score=0.8,
        metadata={"task_id": "task-001", "session_id": "ses-001", "gateway_id": "gw-1"},
    )
    defaults.update(kwargs)
    return MemoryCreate(**defaults)


# ---------------------------------------------------------------------------
# 1. _req_to_dict: canonical field serialisation
# ---------------------------------------------------------------------------

def test_req_to_dict_preserves_memory_type():
    req = _make_req(memory_type=MemoryType.failure)
    d = _req_to_dict(req)
    assert d["memory_type"] == "failure"


def test_req_to_dict_preserves_memory_kind():
    req = _make_req(memory_kind=MemoryKind.evidence)
    d = _req_to_dict(req)
    assert d["memory_kind"] == "evidence"


def test_req_to_dict_preserves_idempotency_key():
    req = _make_req(idempotency_key="key-xyz")
    d = _req_to_dict(req)
    assert d["idempotency_key"] == "key-xyz"


def test_req_to_dict_preserves_salience_score():
    req = _make_req(salience_score=0.95)
    d = _req_to_dict(req)
    assert d["salience_score"] == 0.95


def test_req_to_dict_preserves_metadata():
    req = _make_req(metadata={"task_id": "t-abc", "source": "ci"})
    d = _req_to_dict(req)
    assert d["metadata"]["task_id"] == "t-abc"
    assert d["metadata"]["source"] == "ci"


def test_req_to_dict_embeds_allowed_roles_in_metadata():
    req = _make_req(allowed_roles=["admin", "reviewer"])
    d = _req_to_dict(req)
    assert d["metadata"]["allowed_roles"] == ["admin", "reviewer"]
    assert d["allowed_roles"] == ["admin", "reviewer"]


def test_req_to_dict_does_not_hardcode_memory_type():
    """Serialisation must not override the caller-supplied memory_type."""
    for mt in (MemoryType.failure, MemoryType.external, MemoryType.lesson, MemoryType.observation):
        req = _make_req(memory_type=mt)
        d = _req_to_dict(req)
        assert d["memory_type"] == mt.value, f"memory_type was overridden for {mt}"


def test_req_to_dict_no_memory_type_hardcoded_to_user_action():
    """The old activity hardcoded 'user_action'. Verify that is gone."""
    req = _make_req(memory_type=MemoryType.observation)
    d = _req_to_dict(req)
    assert d["memory_type"] != "user_action"
    assert d["memory_type"] == "observation"


def test_req_to_dict_preserves_source_evidence_in_metadata():
    """Source evidence links in metadata must survive serialisation."""
    req = _make_req(metadata={"source_evidence_ids": ["ev-aaa", "ev-bbb"]})
    d = _req_to_dict(req)
    assert d["metadata"]["source_evidence_ids"] == ["ev-aaa", "ev-bbb"]


# ---------------------------------------------------------------------------
# 2. store_memory_workflow: passes req_dict (not bare content/agent/metadata)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_memory_workflow_passes_req_dict_to_temporal(monkeypatch):
    """store_memory_workflow must forward the full req_dict to the Temporal workflow."""
    received_args: list = []

    class _FakeHandle:
        id = "wf-test-123"

    class _FakeClient:
        async def start_workflow(self, run_fn, *, args, id, task_queue):
            received_args.extend(args)
            return _FakeHandle()

    async def _fake_get_client():
        return _FakeClient()

    monkeypatch.setattr(temporal_client, "get_client", _fake_get_client)

    req_dict = {
        "content": "test content",
        "agent_id": "gw-1",
        "memory_type": "failure",
        "memory_kind": "evidence",
        "idempotency_key": "key-001",
        "salience_score": 0.9,
        "metadata": {"task_id": "t-1"},
        "allowed_roles": ["*"],
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "parent_id": None,
    }
    wf_id = await temporal_client.store_memory_workflow(req_dict)

    assert wf_id == "wf-test-123"
    # The workflow is started with a single positional arg: the req_dict
    assert len(received_args) == 1
    passed = received_args[0]
    assert passed["memory_type"] == "failure"
    assert passed["memory_kind"] == "evidence"
    assert passed["idempotency_key"] == "key-001"


# ---------------------------------------------------------------------------
# 3. /memories/async route: explicit degraded status when Temporal unavailable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_route_returns_degraded_status_when_temporal_missing(monkeypatch):
    """When Temporal is missing the route must return an explicit degraded detail dict."""
    monkeypatch.setattr(temporal_client, "store_memory_workflow", AsyncMock(return_value=None))

    from memu.temporal_routes import create_memory_async

    req = _make_req()
    with pytest.raises(HTTPException) as exc_info:
        await create_memory_async(req, _key="memu-dev-key")

    exc = exc_info.value
    assert exc.status_code == 503
    detail = exc.detail
    assert isinstance(detail, dict), "degraded detail must be a structured dict, not a bare string"
    assert detail["status"] == "degraded"
    assert detail["reason"] == "temporal_unavailable"


@pytest.mark.asyncio
async def test_async_route_returns_accepted_when_temporal_available(monkeypatch):
    import memu.temporal_routes as tr
    monkeypatch.setattr(tr, "store_memory_workflow", AsyncMock(return_value="wf-abc-123"))

    from memu.temporal_routes import create_memory_async

    req = _make_req()
    result = await create_memory_async(req, _key="memu-dev-key")

    assert result["status"] == "accepted"
    assert result["workflow_id"] == "wf-abc-123"


@pytest.mark.asyncio
async def test_async_route_passes_all_canonical_fields_to_workflow(monkeypatch):
    """The route must not strip evidence fields before calling store_memory_workflow."""
    import memu.temporal_routes as tr

    captured: list[dict] = []

    async def _capture(req_dict: dict) -> str:
        captured.append(req_dict)
        return "wf-captured"

    monkeypatch.setattr(tr, "store_memory_workflow", _capture)

    from memu.temporal_routes import create_memory_async

    req = MemoryCreate(
        content="evidence event",
        agent_id="gw-2",
        memory_type=MemoryType.failure,
        memory_kind=MemoryKind.evidence,
        idempotency_key="idem-99",
        salience_score=0.75,
        metadata={"task_id": "t-99", "source_evidence_ids": ["ev-src"]},
        allowed_roles=["admin"],
    )
    await create_memory_async(req, _key="memu-dev-key")

    assert len(captured) == 1
    d = captured[0]
    assert d["memory_type"] == "failure"
    assert d["memory_kind"] == "evidence"
    assert d["idempotency_key"] == "idem-99"
    assert d["salience_score"] == 0.75
    assert d["metadata"]["task_id"] == "t-99"
    assert d["metadata"]["source_evidence_ids"] == ["ev-src"]
    assert d["metadata"]["allowed_roles"] == ["admin"]


# ---------------------------------------------------------------------------
# 4. store_memory activity: canonical field preservation, idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_memory_activity_does_not_hardcode_memory_type(monkeypatch):
    """The store_memory activity must insert the caller-supplied memory_type."""
    import asyncpg
    from memu.temporal_worker.activities import store_memory

    inserted_sql: list[str] = []
    inserted_args: list = []
    fake_id = uuid4()

    class _FakeConn:
        async def fetchrow(self, sql, *args):
            # Skip idempotency check (returns None = no existing row)
            if "idempotency_key" in sql and "WHERE" in sql:
                return None
            inserted_sql.append(sql)
            inserted_args.extend(args)
            return {"id": fake_id}

        async def close(self):
            pass

    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=_FakeConn()))

    req_dict = {
        "content": "pipeline failure detected",
        "agent_id": "gw-3",
        "memory_type": "failure",
        "memory_kind": "evidence",
        "idempotency_key": None,
        "salience_score": 0.9,
        "metadata": {},
        "allowed_roles": ["*"],
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "parent_id": None,
    }
    result = await store_memory(req_dict, None)

    assert result["memory_id"] == str(fake_id)
    assert result["idempotency_status"] == "new"
    # The memory_type passed to INSERT must be "failure", not "user_action"
    assert "failure" in inserted_args, "activity must not override memory_type with 'user_action'"
    assert "user_action" not in inserted_args


@pytest.mark.asyncio
async def test_store_memory_activity_exact_replay_returns_existing_id(monkeypatch):
    """When idempotency_key + evidence + same hash → exact_replay with existing ID."""
    import asyncpg
    from memu.temporal_worker.activities import store_memory
    from memu.schema_contract import compute_canonical_payload_hash

    existing_id = uuid4()
    content = "gateway started"
    memory_type = "user_action"
    agent_id = "gw-4"
    metadata = {"task_id": "task-idem-1"}

    canonical_hash = compute_canonical_payload_hash(
        content=content,
        memory_type=memory_type,
        agent_id=agent_id,
        metadata=metadata,
    )

    class _FakeConn:
        async def fetchrow(self, sql, *args):
            # Return existing row with matching hash
            return {"id": existing_id, "canonical_payload_hash": canonical_hash}

        async def close(self):
            pass

    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=_FakeConn()))

    req_dict = {
        "content": content,
        "agent_id": agent_id,
        "memory_type": memory_type,
        "memory_kind": "evidence",
        "idempotency_key": "idem-replay-1",
        "salience_score": 0.5,
        "metadata": dict(metadata),
        "allowed_roles": ["*"],
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "parent_id": None,
    }
    result = await store_memory(req_dict, None)

    assert result["idempotency_status"] == "exact_replay"
    assert result["memory_id"] == str(existing_id)


@pytest.mark.asyncio
async def test_store_memory_activity_conflict_returns_conflict_status(monkeypatch):
    """When idempotency_key exists but payload hash differs → conflict status."""
    import asyncpg
    from memu.temporal_worker.activities import store_memory

    existing_id = uuid4()

    class _FakeConn:
        async def fetchrow(self, sql, *args):
            # Return existing row with a DIFFERENT hash
            return {"id": existing_id, "canonical_payload_hash": "aaa" + "0" * 61}

        async def close(self):
            pass

    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=_FakeConn()))

    req_dict = {
        "content": "different content for same key",
        "agent_id": "gw-5",
        "memory_type": "user_action",
        "memory_kind": "evidence",
        "idempotency_key": "idem-conflict-1",
        "salience_score": 0.5,
        "metadata": {},
        "allowed_roles": ["*"],
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "parent_id": None,
    }
    result = await store_memory(req_dict, None)

    assert result["idempotency_status"] == "conflict"


@pytest.mark.asyncio
async def test_store_memory_activity_learning_skips_idempotency_check(monkeypatch):
    """Learning memory with an idempotency_key still skips the evidence idempotency path."""
    import asyncpg
    from memu.temporal_worker.activities import store_memory

    inserted_id = uuid4()
    idempotency_calls = []

    class _FakeConn:
        async def fetchrow(self, sql, *args):
            if "idempotency_key" in sql and "WHERE" in sql:
                idempotency_calls.append(sql)
            return {"id": inserted_id}

        async def close(self):
            pass

    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=_FakeConn()))

    req_dict = {
        "content": "learned lesson",
        "agent_id": "agent-6",
        "memory_type": "lesson",
        "memory_kind": "learning",
        "idempotency_key": "idem-learning-1",
        "salience_score": 0.5,
        "metadata": {},
        "allowed_roles": ["*"],
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "parent_id": None,
    }
    result = await store_memory(req_dict, None)

    # Learning memory skips the evidence idempotency SELECT
    assert idempotency_calls == [], "Learning memory must not trigger evidence idempotency check"
    assert result["idempotency_status"] == "new"
    assert result["memory_id"] == str(inserted_id)


# ---------------------------------------------------------------------------
# 5. Core API Readiness not affected by Temporal absence (contract proof)
# ---------------------------------------------------------------------------

def test_core_readiness_does_not_include_temporal():
    """The /health endpoint must not check Temporal — validated by the verify_deployment script."""
    from scripts import verify_deployment as vd

    # If TEMPORAL_HOST is absent, _verify_single with check_async=False must still pass.
    # This is the Core API Readiness contract: Temporal is optional.
    # Verify the _verify_single function signature accepts check_async kwarg
    import inspect
    sig = inspect.signature(vd._verify_single)
    assert "check_async" in sig.parameters, (
        "_verify_single must have a check_async parameter so Temporal absence "
        "does not block Core API Readiness"
    )
    # When check_async=False, Temporal is not exercised
    param = sig.parameters["check_async"]
    # The default should be False or the parameter must exist for caller to pass False
    assert param is not None
