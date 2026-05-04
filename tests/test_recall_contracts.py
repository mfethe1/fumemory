"""Tests for learning recall and forensic recall contracts (Issue #27).

Coverage:
- Default recall (/api/v1/recall) returns only Learning Memory
  (memory_kind='learning', review_status IN accepted/legacy/accepted_by_timeout)
- Default recall excludes evidence rows and non-eligible review statuses
- Default recall lexical fallback when embedding unavailable
- Forensic recall (/api/v1/recall/forensic) returns evidence with provenance
- Forensic recall supports task_id/session_id/gateway_id/agent_id/event_type filters
- Forensic recall cursor-based pagination
- Forensic recall redaction: role_access_denied and content_excluded_by_request
- Forensic recall artifact_ref filter
- _has_role_access helper
- _build_forensic_item helper
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID

import pytest

from memu import api
from memu.api import _has_role_access, _build_forensic_item
from memu.models import (
    ForensicRecallItem,
    ForensicRecallRequest,
    ForensicRecallResponse,
    RecallMode,
    SearchRequest,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _fake_tenant_conn(conn):
    @asynccontextmanager
    async def _ctx(_auth):
        yield conn
    return _ctx


def _make_memory_row(
    *,
    id=None,
    content="some lesson content",
    memory_type="lesson",
    memory_kind="learning",
    review_status="legacy",
    agent_id="agent-1",
    metadata=None,
    salience_score=0.5,
):
    now = datetime.now(timezone.utc)
    return {
        "id": id or uuid4(),
        "content": content,
        "memory_type": memory_type,
        "memory_kind": memory_kind,
        "review_status": review_status,
        "agent_id": agent_id,
        "metadata": metadata if metadata is not None else {"allowed_roles": ["*"]},
        "parent_id": None,
        "confidence": 1.0,
        "access_count": 0,
        "decay_score": 1.0,
        "salience_score": salience_score,
        "searchable": True,
        "similarity": 0.85,
        "canonical_payload_hash": None,
        "idempotency_key": None,
        "valid_to": None,
        "created_at": now,
        "updated_at": now,
    }


def _make_evidence_row(
    *,
    id=None,
    content="gateway executed task",
    memory_type="user_action",
    agent_id="gw-1",
    metadata=None,
):
    now = datetime.now(timezone.utc)
    meta = metadata if metadata is not None else {
        "task_id": "task-001",
        "session_id": "ses-001",
        "gateway_id": "gw-1",
        "event_type": "task_complete",
        "allowed_roles": ["*"],
        "artifact_refs": ["artifact-001"],
    }
    return {
        "id": id or uuid4(),
        "content": content,
        "memory_type": memory_type,
        "memory_kind": "evidence",
        "review_status": None,
        "agent_id": agent_id,
        "metadata": meta,
        "parent_id": None,
        "confidence": 1.0,
        "access_count": 0,
        "decay_score": 1.0,
        "salience_score": 0.8,
        "searchable": True,
        "similarity": 0.0,
        "canonical_payload_hash": None,
        "idempotency_key": None,
        "valid_to": None,
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# 1. _has_role_access helper
# ---------------------------------------------------------------------------

def test_has_role_access_wildcard_allows_all():
    assert _has_role_access(["*"], ["some-role"]) is True


def test_has_role_access_wildcard_allows_no_roles():
    assert _has_role_access(["*"], []) is True


def test_has_role_access_none_agent_roles_skips_filter():
    assert _has_role_access(["admin"], None) is True


def test_has_role_access_matching_role_grants_access():
    assert _has_role_access(["admin", "reviewer"], ["reviewer"]) is True


def test_has_role_access_no_match_denies_access():
    assert _has_role_access(["admin"], ["reviewer"]) is False


def test_has_role_access_empty_allowed_roles_denies_all():
    assert _has_role_access([], ["admin"]) is False


# ---------------------------------------------------------------------------
# 2. _build_forensic_item helper
# ---------------------------------------------------------------------------

def test_build_forensic_item_basic():
    row = _make_evidence_row()
    item = _build_forensic_item(row, include_content=True, agent_roles=None)

    assert item.evidence_id == row["id"]
    assert item.memory_kind == "evidence"
    assert item.content == row["content"]
    assert item.redacted is False
    assert item.redaction_reason is None
    assert item.task_id == "task-001"
    assert item.session_id == "ses-001"
    assert item.gateway_id == "gw-1"
    assert item.event_type == "task_complete"
    assert item.artifact_refs == ["artifact-001"]


def test_build_forensic_item_redacts_when_include_content_false():
    row = _make_evidence_row()
    item = _build_forensic_item(row, include_content=False, agent_roles=None)

    assert item.content is None
    assert item.redacted is True
    assert item.redaction_reason == "content_excluded_by_request"


def test_build_forensic_item_redacts_on_role_mismatch():
    row = _make_evidence_row(metadata={
        "allowed_roles": ["admin"],
        "task_id": "t1",
    })
    item = _build_forensic_item(row, include_content=True, agent_roles=["reviewer"])

    assert item.content is None
    assert item.redacted is True
    assert item.redaction_reason == "role_access_denied"
    # Audit fields still present despite redaction
    assert item.task_id == "t1"
    assert item.evidence_id == row["id"]


def test_build_forensic_item_allows_matching_role():
    row = _make_evidence_row(metadata={"allowed_roles": ["admin", "reviewer"]})
    item = _build_forensic_item(row, include_content=True, agent_roles=["reviewer"])

    assert item.redacted is False
    assert item.content == row["content"]


def test_build_forensic_item_extracts_provenance_links():
    row = _make_evidence_row(metadata={
        "allowed_roles": ["*"],
        "source_evidence_ids": ["ev-aaa", "ev-bbb"],
    })
    item = _build_forensic_item(row, include_content=True, agent_roles=None)
    assert "ev-aaa" in item.provenance_links
    assert "ev-bbb" in item.provenance_links


# ---------------------------------------------------------------------------
# 3. Default (learning) recall — returns only Learning Memory
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_learning_recall_sql_filters_memory_kind_and_review_status(monkeypatch):
    """The /api/v1/recall SQL must include memory_kind=learning and review_status filter."""
    learning_row = _make_memory_row(review_status="legacy")

    conn = AsyncMock()
    # No embedding → falls back to lexical; conn.fetch returns one row
    conn.fetch.return_value = [learning_row]
    conn.execute = AsyncMock()

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = SearchRequest(query="lesson about embedding", limit=5)
    results = await api.learning_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert len(results) == 1
    fetch_sql = conn.fetch.await_args.args[0]
    assert "memory_kind = 'learning'" in fetch_sql
    assert "review_status IN" in fetch_sql
    assert "accepted" in fetch_sql
    assert "legacy" in fetch_sql
    assert "accepted_by_timeout" in fetch_sql


@pytest.mark.asyncio
async def test_learning_recall_excludes_evidence_memory(monkeypatch):
    """If the only DB records are evidence kind, learning recall returns nothing."""
    conn = AsyncMock()
    conn.fetch.return_value = []  # No matching learning records
    conn.execute = AsyncMock()

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = SearchRequest(query="gateway task proof", limit=5)
    results = await api.learning_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert results == []
    fetch_sql = conn.fetch.await_args.args[0]
    # Must not include memory_kind = 'evidence'
    assert "memory_kind = 'learning'" in fetch_sql


@pytest.mark.asyncio
async def test_learning_recall_includes_accepted_review_status(monkeypatch):
    """Accepted learning memory appears in default recall."""
    accepted_row = _make_memory_row(review_status="accepted", memory_type="lesson")

    conn = AsyncMock()
    conn.fetch.return_value = [accepted_row]
    conn.execute = AsyncMock()

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = SearchRequest(query="lesson", limit=5)
    results = await api.learning_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert len(results) == 1
    assert results[0].memory.review_status == "accepted"


@pytest.mark.asyncio
async def test_learning_recall_lexical_fallback_when_embedding_unavailable(monkeypatch):
    """When embedding returns None, learning recall uses ILIKE and still applies filters."""
    learning_row = _make_memory_row(review_status="legacy")

    conn = AsyncMock()
    conn.fetch.return_value = [learning_row]
    conn.execute = AsyncMock()

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = SearchRequest(query="embedding contract lesson", limit=5)
    results = await api.learning_recall(req, _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "ILIKE" in fetch_sql
    assert "memory_kind = 'learning'" in fetch_sql
    assert "review_status IN" in fetch_sql
    assert len(results) == 1


@pytest.mark.asyncio
async def test_learning_recall_vector_path_applies_learning_filters(monkeypatch):
    """When embedding is available, vector query still filters to learning memory only."""
    import numpy as np

    learning_row = _make_memory_row(review_status="accepted_by_timeout")
    fake_embedding = [0.1] * 8

    conn = AsyncMock()
    conn.fetch.return_value = [learning_row]
    conn.execute = AsyncMock()

    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=fake_embedding))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))
    monkeypatch.setattr(api, "EMBEDDING_DIMS", 8)

    req = SearchRequest(query="cross-task pattern", limit=5)
    results = await api.learning_recall(req, _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "memory_kind = 'learning'" in fetch_sql
    assert "review_status IN" in fetch_sql
    assert len(results) == 1
    assert results[0].memory.review_status == "accepted_by_timeout"


# ---------------------------------------------------------------------------
# 4. Forensic Recall endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forensic_recall_returns_evidence_rows(monkeypatch):
    """Forensic recall returns evidence rows from the DB."""
    ev_row = _make_evidence_row()

    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]

    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(limit=10)
    resp = await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert isinstance(resp, ForensicRecallResponse)
    assert len(resp.items) == 1
    assert resp.items[0].memory_kind == "evidence"
    assert resp.items[0].evidence_id == ev_row["id"]
    assert resp.items[0].redacted is False

    fetch_sql = conn.fetch.await_args.args[0]
    assert "memory_kind = 'evidence'" in fetch_sql


@pytest.mark.asyncio
async def test_forensic_recall_filters_by_task_id(monkeypatch):
    """task_id filter is applied to the WHERE clause."""
    ev_row = _make_evidence_row()
    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(task_id="task-001", limit=10)
    await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "metadata->>'task_id'" in fetch_sql
    assert "task-001" in conn.fetch.await_args.args


@pytest.mark.asyncio
async def test_forensic_recall_filters_by_session_and_gateway(monkeypatch):
    """session_id and gateway_id filters are applied to the WHERE clause."""
    ev_row = _make_evidence_row()
    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(session_id="ses-001", gateway_id="gw-1", limit=10)
    await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "metadata->>'session_id'" in fetch_sql
    assert "metadata->>'gateway_id'" in fetch_sql
    args = conn.fetch.await_args.args
    assert "ses-001" in args
    assert "gw-1" in args


@pytest.mark.asyncio
async def test_forensic_recall_filters_by_agent_id(monkeypatch):
    ev_row = _make_evidence_row(agent_id="gw-special")
    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(agent_id="gw-special", limit=10)
    await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "agent_id = " in fetch_sql
    assert "gw-special" in conn.fetch.await_args.args


@pytest.mark.asyncio
async def test_forensic_recall_filters_by_event_type(monkeypatch):
    ev_row = _make_evidence_row()
    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(event_type="task_complete", limit=10)
    await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "metadata->>'event_type'" in fetch_sql
    assert "task_complete" in conn.fetch.await_args.args


@pytest.mark.asyncio
async def test_forensic_recall_filters_by_artifact_ref(monkeypatch):
    ev_row = _make_evidence_row()
    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(artifact_ref="artifact-001", limit=10)
    await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "artifact_refs" in fetch_sql


@pytest.mark.asyncio
async def test_forensic_recall_pagination_sets_next_cursor(monkeypatch):
    """When DB returns limit+1 rows, next_cursor is set on the response."""
    rows = [_make_evidence_row() for _ in range(6)]  # limit=5, so 6 triggers next page

    conn = AsyncMock()
    conn.fetch.return_value = rows
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(limit=5)
    resp = await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert resp.next_cursor is not None
    assert len(resp.items) == 5  # Only limit items returned


@pytest.mark.asyncio
async def test_forensic_recall_no_next_cursor_on_last_page(monkeypatch):
    """When DB returns ≤ limit rows, next_cursor is None."""
    rows = [_make_evidence_row() for _ in range(3)]

    conn = AsyncMock()
    conn.fetch.return_value = rows
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(limit=5)
    resp = await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert resp.next_cursor is None
    assert len(resp.items) == 3


@pytest.mark.asyncio
async def test_forensic_recall_cursor_pagination_adds_where_clause(monkeypatch):
    """A cursor in the request adds a (created_at, id) keyset filter to the SQL."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    cursor = "2026-05-04T10:00:00+00:00__some-uuid-value"
    req = ForensicRecallRequest(limit=5, cursor=cursor)
    resp = await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "created_at" in fetch_sql


@pytest.mark.asyncio
async def test_forensic_recall_invalid_cursor_raises_400(monkeypatch):
    from fastapi import HTTPException

    conn = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(limit=5, cursor="bad-cursor-no-separator")
    with pytest.raises(HTTPException) as exc_info:
        await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_forensic_recall_redacts_on_role_mismatch(monkeypatch):
    """Evidence content is redacted when caller lacks required role."""
    ev_row = _make_evidence_row(
        metadata={"allowed_roles": ["admin"], "task_id": "t1"}
    )
    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(limit=10, agent_roles=["reviewer"])
    resp = await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert len(resp.items) == 1
    item = resp.items[0]
    assert item.redacted is True
    assert item.redaction_reason == "role_access_denied"
    assert item.content is None
    # Audit fields still present
    assert item.evidence_id == ev_row["id"]
    assert item.task_id == "t1"


@pytest.mark.asyncio
async def test_forensic_recall_redacts_when_include_content_false(monkeypatch):
    """include_content=False redacts all evidence content regardless of roles."""
    ev_row = _make_evidence_row()
    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(limit=10, include_content=False)
    resp = await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    item = resp.items[0]
    assert item.redacted is True
    assert item.content is None
    assert item.redaction_reason == "content_excluded_by_request"


@pytest.mark.asyncio
async def test_forensic_recall_response_includes_provenance_fields(monkeypatch):
    """Forensic recall items include task/session/gateway/agent provenance."""
    ev_row = _make_evidence_row(metadata={
        "task_id": "t-100",
        "session_id": "s-200",
        "gateway_id": "gw-300",
        "event_type": "tool_call",
        "allowed_roles": ["*"],
        "artifact_refs": ["art-001", "art-002"],
        "source_evidence_ids": ["ev-src-1"],
    })
    conn = AsyncMock()
    conn.fetch.return_value = [ev_row]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ForensicRecallRequest(limit=10)
    resp = await api.forensic_recall(req, _key=api.AuthContext("memu-dev-key"))

    item = resp.items[0]
    assert item.task_id == "t-100"
    assert item.session_id == "s-200"
    assert item.gateway_id == "gw-300"
    assert item.event_type == "tool_call"
    assert "art-001" in item.artifact_refs
    assert "art-002" in item.artifact_refs
    assert "ev-src-1" in item.provenance_links


# ---------------------------------------------------------------------------
# 5. Model contract: RecallMode enum
# ---------------------------------------------------------------------------

def test_recall_mode_enum_values():
    assert RecallMode.learning == "learning"
    assert RecallMode.forensic == "forensic"


def test_forensic_recall_request_defaults():
    req = ForensicRecallRequest()
    assert req.limit == 20
    assert req.include_content is True
    assert req.cursor is None
    assert req.agent_roles is None


def test_forensic_recall_response_model():
    resp = ForensicRecallResponse(items=[], next_cursor=None)
    assert resp.items == []
    assert resp.next_cursor is None
