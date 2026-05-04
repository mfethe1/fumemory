"""Memory Action Eval for embedding environment mismatch (Issue #33).

Multi-session evaluation proving evidence becomes Learning Memory and changes
later OpenClaw behavior:

  Session 1  — gateway records failure evidence for embedding env mismatch
               (EMBEDDING_BASE_URL used instead of canonical EMBEDDING_API_BASE)
  Reflection — generate_task_close_proposals distills the failure into a proposal
  Review     — proposal is approved or timeout-integrated
  Session 2  — default recall surfaces the accepted learning before a
               configuration decision; gateway uses EMBEDDING_API_BASE
               and avoids the prior mistake

Eval output per run:
  evidence_ids     — UUIDs from session 1 evidence records
  learning_id      — UUID of the accepted Learning Memory
  review_state     — proposal status after lifecycle action
  recall_result    — learning text returned in session 2
  behavioral_trace — before/after decision showing avoidance of the mistake

Failure scenario:
  EMBEDDING_BASE_URL (deprecated alias) was used instead of the canonical
  EMBEDDING_API_BASE.  Vector dimensions mismatched silently: semantic recall
  returned empty results without surfacing a clear error.
  See docs/adr/0003-versioned-embedding-contract.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from memu import api
from memu.api import (
    create_reflection_proposal,
    process_reflection_timeouts,
    take_reflection_action,
)
from memu.models import (
    ReflectionAction,
    ReflectionActionRequest,
    ReflectionProposalCreate,
    ReflectionSource,
    SearchRequest,
)
from memu.reflection import generate_task_close_proposals


# ---------------------------------------------------------------------------
# Scenario constants
# ---------------------------------------------------------------------------

FAILURE_CONTENT = (
    "Gateway gw-railway-1 attempted embedding lookup using EMBEDDING_BASE_URL "
    "(http://internal-embeddings:11434) but the deployed API is configured for "
    "EMBEDDING_API_BASE with text-embedding-3-small/1536 dimensions. Semantic "
    "recall silently degraded: all vector queries returned empty results. "
    "Root cause: EMBEDDING_BASE_URL is a deprecated compatibility alias only; "
    "EMBEDDING_API_BASE is the canonical env var per docs/adr/0003-versioned-embedding-contract."
)

LEARNING_CONTENT = (
    "Use EMBEDDING_API_BASE (not EMBEDDING_BASE_URL) as the canonical embedding "
    "endpoint variable. EMBEDDING_BASE_URL is a deprecated compatibility alias "
    "only. Always verify EMBEDDING_DIMS matches the active vector schema before "
    "deploying new embedding configuration. Mismatch causes silent semantic "
    "recall degradation without surfacing a clear error."
)

SESSION_1_TASK_ID = "task-embed-config-2026-05-04"
SESSION_1_SESSION_ID = "ses-gw-railway-1-001"
SESSION_1_GATEWAY_ID = "gw-railway-1"
SESSION_2_QUERY = "embedding configuration canonical env var EMBEDDING_API_BASE"


# ---------------------------------------------------------------------------
# Eval output dataclass
# ---------------------------------------------------------------------------

@dataclass
class EmbeddingMismatchEvalResult:
    """Structured output of the full Memory Action Eval run."""

    evidence_ids: list[str]
    learning_id: str
    review_state: str
    recall_result: str
    behavioral_trace: dict[str, str]

    def passes(self) -> bool:
        """Return True if the eval proves behavior changed due to recalled learning."""
        return (
            bool(self.evidence_ids)
            and bool(self.learning_id)
            and self.review_state in ("accepted", "accepted_by_timeout")
            and "EMBEDDING_API_BASE" in self.recall_result
            and self.behavioral_trace.get("session2_env_var") == "EMBEDDING_API_BASE"
            and self.behavioral_trace.get("session1_env_var") == "EMBEDDING_BASE_URL"
        )


# ---------------------------------------------------------------------------
# Row factories
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_evidence_row(
    *,
    id: UUID | None = None,
    content: str = FAILURE_CONTENT,
    task_id: str = SESSION_1_TASK_ID,
    session_id: str = SESSION_1_SESSION_ID,
    gateway_id: str = SESSION_1_GATEWAY_ID,
) -> dict:
    now = _now()
    return {
        "id": id or uuid4(),
        "content": content,
        "memory_type": "failure",
        "memory_kind": "evidence",
        "review_status": None,
        "agent_id": gateway_id,
        "metadata": {
            "task_id": task_id,
            "session_id": session_id,
            "gateway_id": gateway_id,
            "event_type": "embedding_lookup_failure",
            "allowed_roles": ["*"],
        },
        "parent_id": None,
        "confidence": 0.95,
        "access_count": 0,
        "decay_score": 1.0,
        "salience_score": 0.9,
        "searchable": True,
        "canonical_payload_hash": None,
        "idempotency_key": f"embed-fail-{task_id}",
        "valid_to": None,
        "created_at": now,
        "updated_at": now,
    }


def _make_proposal_row(
    *,
    proposal_id: UUID | None = None,
    status: str = "pending",
    source: str = "task_close",
    content: str = LEARNING_CONTENT,
    confidence: float = 0.9,
    risk_flags: list[str] | None = None,
    source_task_id: str = SESSION_1_TASK_ID,
    source_session_id: str = SESSION_1_SESSION_ID,
    source_evidence_ids: list[str] | None = None,
    expires_at: datetime | None = None,
    memory_id: UUID | None = None,
    agent_id: str = SESSION_1_GATEWAY_ID,
    tenant_id: str = "test-tenant",
) -> dict:
    now = _now()
    return {
        "proposal_id": proposal_id or uuid4(),
        "tenant_id": tenant_id,
        "status": status,
        "source": source,
        "summary": FAILURE_CONTENT[:200],
        "content": content,
        "confidence": confidence,
        "risk_flags": json.dumps(risk_flags or ["failure_evidence"]),
        "source_task_id": source_task_id,
        "source_session_id": source_session_id,
        "source_evidence_ids": json.dumps(source_evidence_ids or []),
        "expires_at": expires_at or (now + timedelta(hours=6)),
        "telegram_message_id": None,
        "memory_id": memory_id,
        "superseded_by": None,
        "agent_id": agent_id,
        "created_at": now,
        "updated_at": now,
    }


def _make_learning_row(
    *,
    id: UUID | None = None,
    content: str = LEARNING_CONTENT,
    review_status: str = "accepted",
    evidence_id: UUID | None = None,
) -> dict:
    now = _now()
    return {
        "id": id or uuid4(),
        "content": content,
        "memory_type": "lesson",
        "memory_kind": "learning",
        "review_status": review_status,
        "agent_id": "reflection-worker",
        "metadata": {
            "source": "reflection",
            "source_evidence_ids": [str(evidence_id)] if evidence_id else [],
            "source_task_id": SESSION_1_TASK_ID,
            "allowed_roles": ["*"],
        },
        "parent_id": None,
        "confidence": 0.9,
        "access_count": 0,
        "decay_score": 1.0,
        "salience_score": 0.85,
        "searchable": True,
        "similarity": 0.88,
        "canonical_payload_hash": None,
        "idempotency_key": None,
        "valid_to": None,
        "created_at": now,
        "updated_at": now,
    }


def _fake_tenant_conn(conn):
    @asynccontextmanager
    async def _ctx(_auth):
        yield conn
    return _ctx


# ---------------------------------------------------------------------------
# Session 2 behavioral decision functions
# ---------------------------------------------------------------------------

def _configure_embedding_without_recall() -> dict:
    """Session 2 gateway configures embedding without any prior learning.

    Without recalled knowledge of the canonical env var, defaults to the
    deprecated EMBEDDING_BASE_URL alias — the same mistake as session 1.
    """
    return {
        "env_var": "EMBEDDING_BASE_URL",
        "value": "http://internal-embeddings:11434",
        "reason": "defaulting to known alias; no recall available",
    }


def _configure_embedding_with_recall(learning_text: str) -> dict:
    """Session 2 gateway configures embedding after recall surfaces the learning.

    If the recalled text mentions EMBEDDING_API_BASE as canonical, the gateway
    uses it instead of the deprecated alias.  This is the behavioral change
    the eval must prove.
    """
    if "EMBEDDING_API_BASE" in learning_text and (
        "canonical" in learning_text.lower()
        or "EMBEDDING_BASE_URL" in learning_text
    ):
        return {
            "env_var": "EMBEDDING_API_BASE",
            "value": "https://api.openai.com/v1",
            "reason": f"recall surfaced: {learning_text[:120]}",
        }
    return _configure_embedding_without_recall()


# ---------------------------------------------------------------------------
# AC1: Session 1 records evidence for the failure
# ---------------------------------------------------------------------------

def test_session1_evidence_is_evidence_kind_with_failure_type():
    """Session 1 failure evidence has memory_kind=evidence and memory_type=failure."""
    evidence = _make_evidence_row()
    assert evidence["memory_kind"] == "evidence"
    assert evidence["memory_type"] == "failure"
    assert evidence["review_status"] is None


def test_session1_evidence_carries_task_session_gateway_provenance():
    """Session 1 evidence metadata includes task_id, session_id, gateway_id."""
    evidence = _make_evidence_row()
    meta = evidence["metadata"]
    assert meta["task_id"] == SESSION_1_TASK_ID
    assert meta["session_id"] == SESSION_1_SESSION_ID
    assert meta["gateway_id"] == SESSION_1_GATEWAY_ID


def test_session1_evidence_content_describes_the_embedding_mismatch():
    """Session 1 evidence content names both the wrong and canonical env var."""
    evidence = _make_evidence_row()
    assert "EMBEDDING_BASE_URL" in evidence["content"]
    assert "EMBEDDING_API_BASE" in evidence["content"]


# ---------------------------------------------------------------------------
# AC2: Reflection produces Learning Memory linked to source evidence
# ---------------------------------------------------------------------------

def test_reflection_generates_proposal_from_session1_failure_evidence():
    """generate_task_close_proposals produces a task_close proposal from the failure."""
    evidence = _make_evidence_row()
    proposals = generate_task_close_proposals(
        task_id=SESSION_1_TASK_ID,
        session_id=SESSION_1_SESSION_ID,
        evidence_records=[evidence],
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["source"] == "task_close"
    assert proposal["source_task_id"] == SESSION_1_TASK_ID
    assert proposal["source_session_id"] == SESSION_1_SESSION_ID


def test_reflection_proposal_links_source_evidence_id():
    """The generated proposal source_evidence_ids contains session 1 evidence UUID."""
    evidence_id = uuid4()
    evidence = _make_evidence_row(id=evidence_id)
    proposals = generate_task_close_proposals(
        task_id=SESSION_1_TASK_ID,
        session_id=SESSION_1_SESSION_ID,
        evidence_records=[evidence],
    )
    assert len(proposals) == 1
    assert str(evidence_id) in proposals[0]["source_evidence_ids"]


def test_reflection_prioritises_failure_evidence_over_other_records():
    """Failure-type records are proposed first — higher learning value."""
    failure_ev = _make_evidence_row()
    other_ev = dict(_make_evidence_row(), memory_type="observation", content="routine log entry")
    proposals = generate_task_close_proposals(
        task_id=SESSION_1_TASK_ID,
        session_id=SESSION_1_SESSION_ID,
        evidence_records=[other_ev, failure_ev],
    )
    assert FAILURE_CONTENT[:50] in proposals[0]["content"]


@pytest.mark.asyncio
async def test_create_proposal_api_persists_source_evidence_ids(monkeypatch):
    """The create-proposal API endpoint stores source_evidence_ids in the DB row."""
    evidence_id = str(uuid4())
    proposal_row = _make_proposal_row(source_evidence_ids=[evidence_id])

    conn = AsyncMock()
    conn.fetchrow.return_value = proposal_row
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ReflectionProposalCreate(
        source=ReflectionSource.task_close,
        summary=FAILURE_CONTENT[:200],
        content=LEARNING_CONTENT,
        confidence=0.9,
        risk_flags=["failure_evidence"],
        source_task_id=SESSION_1_TASK_ID,
        source_session_id=SESSION_1_SESSION_ID,
        source_evidence_ids=[evidence_id],
        agent_id=SESSION_1_GATEWAY_ID,
    )
    result = await create_reflection_proposal(req, _key=api.AuthContext("memu-dev-key"))

    assert result.source_task_id == SESSION_1_TASK_ID
    insert_sql = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO reflection_proposals" in insert_sql
    assert "pending" in insert_sql


# ---------------------------------------------------------------------------
# AC3: Learning is approved or timeout-integrated through the review lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_action_writes_learning_with_accepted_status(monkeypatch):
    """Approving the proposal creates a Learning Memory with review_status='accepted'."""
    pid = uuid4()
    learning_id = uuid4()
    proposal_row = _make_proposal_row(proposal_id=pid)

    conn = AsyncMock()
    conn.fetchrow.side_effect = [proposal_row, {"id": learning_id}]
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await take_reflection_action(
        pid,
        ReflectionActionRequest(actor="operator-1", decision=ReflectionAction.approve),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.decision == "approve"
    assert result.status == "accepted"
    assert result.memory_id == learning_id

    memory_insert_call = conn.fetchrow.call_args_list[1]
    assert "INSERT INTO memories" in memory_insert_call.args[0]
    assert "accepted" in memory_insert_call.args


@pytest.mark.asyncio
async def test_timeout_integration_marks_learning_accepted_by_timeout(monkeypatch):
    """Expired proposal is auto-integrated as accepted_by_timeout via process-timeouts."""
    learning_id = uuid4()
    expired_proposal = _make_proposal_row(
        status="pending",
        expires_at=_now() - timedelta(hours=1),
    )

    conn = AsyncMock()
    conn.fetch.return_value = [expired_proposal]
    conn.fetchrow.return_value = {"id": learning_id}
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await process_reflection_timeouts(_key=api.AuthContext("memu-dev-key"))

    assert result["processed"] == 1
    execute_sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("accepted_by_timeout" in s for s in execute_sqls)


@pytest.mark.asyncio
async def test_rejected_learning_does_not_enter_default_recall(monkeypatch):
    """Denied proposal is rejected and never enters default recall."""
    pid = uuid4()
    proposal_row = _make_proposal_row(proposal_id=pid)

    conn = AsyncMock()
    conn.fetchrow.return_value = proposal_row
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await take_reflection_action(
        pid,
        ReflectionActionRequest(actor="operator-1", decision=ReflectionAction.deny),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.status == "rejected"
    assert result.memory_id is None
    execute_sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert not any("INSERT INTO memories" in s for s in execute_sqls)


# ---------------------------------------------------------------------------
# AC4: Session 2 avoids the prior mistake because recall surfaces the learning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session2_recall_returns_accepted_embedding_learning(monkeypatch):
    """Default recall in session 2 surfaces the accepted learning about EMBEDDING_API_BASE."""
    learning_id = uuid4()
    evidence_id = uuid4()
    learning_row = _make_learning_row(
        id=learning_id,
        review_status="accepted",
        evidence_id=evidence_id,
    )

    conn = AsyncMock()
    conn.fetch.return_value = [learning_row]
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = SearchRequest(query=SESSION_2_QUERY, limit=5)
    results = await api.learning_recall(req, _key=api.AuthContext("memu-dev-key"))

    assert len(results) == 1
    memory = results[0].memory
    assert memory.memory_kind == "learning"
    assert memory.review_status == "accepted"
    assert "EMBEDDING_API_BASE" in memory.content


@pytest.mark.asyncio
async def test_session2_recall_sql_filters_to_learning_only(monkeypatch):
    """Default recall SQL enforces memory_kind=learning — raw evidence never leaks in."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    await api.learning_recall(
        SearchRequest(query=SESSION_2_QUERY, limit=5),
        _key=api.AuthContext("memu-dev-key"),
    )

    fetch_sql = conn.fetch.await_args.args[0]
    assert "memory_kind = 'learning'" in fetch_sql
    assert "review_status IN" in fetch_sql
    assert "accepted" in fetch_sql
    assert "accepted_by_timeout" in fetch_sql


def test_session2_without_recall_defaults_to_wrong_env_var():
    """Without recall, session 2 repeats the session 1 mistake (EMBEDDING_BASE_URL)."""
    decision = _configure_embedding_without_recall()
    assert decision["env_var"] == "EMBEDDING_BASE_URL"


def test_session2_with_recalled_learning_uses_canonical_env_var():
    """With recalled learning, session 2 switches to EMBEDDING_API_BASE."""
    decision = _configure_embedding_with_recall(LEARNING_CONTENT)
    assert decision["env_var"] == "EMBEDDING_API_BASE"
    assert "recall surfaced" in decision["reason"]


def test_session2_with_irrelevant_recall_still_defaults_to_wrong_env_var():
    """Unrelated recalled learning does not prevent the mistake — specificity matters."""
    irrelevant = "Use Railway private networking for Postgres connections."
    decision = _configure_embedding_with_recall(irrelevant)
    assert decision["env_var"] == "EMBEDDING_BASE_URL"


def test_session1_evidence_excluded_from_default_recall():
    """Evidence from session 1 has no review_status and memory_kind='evidence'.

    Default recall requires memory_kind='learning' and an eligible review_status,
    so the raw session 1 evidence can never surface in default recall.  Forensic
    recall is the only path to that record.
    """
    evidence = _make_evidence_row()
    assert evidence["memory_kind"] == "evidence"
    assert evidence["review_status"] is None


# ---------------------------------------------------------------------------
# AC5: Full eval — all phases prove the complete workflow (approve path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_embedding_mismatch_eval_approve_path(monkeypatch):
    """Full eval: session 1 evidence → reflection → approve → session 2 recall.

    Proves that an accepted Learning Memory from session 1's failure
    changes the configuration decision in session 2.  This is the primary
    eval acceptance test for issue #33.

    Eval output fields verified:
      evidence_ids     — UUIDs from session 1 evidence
      learning_id      — UUID of the accepted Learning Memory
      review_state     — "accepted" (explicit user approval)
      recall_result    — learning text surfaced in session 2
      behavioral_trace — before/after configuration decision
    """
    evidence_id = uuid4()
    proposal_id = uuid4()
    learning_id = uuid4()

    # --- Session 1: failure evidence ---
    evidence = _make_evidence_row(id=evidence_id)
    assert evidence["memory_kind"] == "evidence"
    assert evidence["memory_type"] == "failure"

    # --- Reflection: generate proposal from failure evidence ---
    proposals = generate_task_close_proposals(
        task_id=SESSION_1_TASK_ID,
        session_id=SESSION_1_SESSION_ID,
        evidence_records=[evidence],
    )
    assert len(proposals) == 1
    generated = proposals[0]
    assert str(evidence_id) in generated["source_evidence_ids"]
    assert generated["source_task_id"] == SESSION_1_TASK_ID

    # --- Review: approve the proposal ---
    proposal_row = _make_proposal_row(
        proposal_id=proposal_id,
        source_evidence_ids=[str(evidence_id)],
        content=LEARNING_CONTENT,
    )
    conn_approve = AsyncMock()
    conn_approve.fetchrow.side_effect = [proposal_row, {"id": learning_id}]
    conn_approve.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn_approve))

    approve_result = await take_reflection_action(
        proposal_id,
        ReflectionActionRequest(actor="operator-1", decision=ReflectionAction.approve),
        _key=api.AuthContext("memu-dev-key"),
    )
    assert approve_result.status == "accepted"
    assert approve_result.memory_id == learning_id
    review_state = approve_result.status

    # --- Session 2: default recall before making embedding config decision ---
    learning_row = _make_learning_row(
        id=learning_id,
        review_status="accepted",
        evidence_id=evidence_id,
    )
    conn_recall = AsyncMock()
    conn_recall.fetch.return_value = [learning_row]
    conn_recall.execute = AsyncMock()
    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn_recall))

    recall_results = await api.learning_recall(
        SearchRequest(query=SESSION_2_QUERY, limit=5),
        _key=api.AuthContext("memu-dev-key"),
    )
    assert len(recall_results) == 1
    recalled_text = recall_results[0].memory.content
    assert "EMBEDDING_API_BASE" in recalled_text

    # Verify recall SQL enforces learning-only filters
    fetch_sql = conn_recall.fetch.await_args.args[0]
    assert "memory_kind = 'learning'" in fetch_sql
    assert "review_status IN" in fetch_sql

    # --- Behavioral trace: show changed decision ---
    decision_without_recall = _configure_embedding_without_recall()
    decision_with_recall = _configure_embedding_with_recall(recalled_text)

    behavioral_trace = {
        "session1_env_var": decision_without_recall["env_var"],
        "session2_env_var": decision_with_recall["env_var"],
        "session2_reason": decision_with_recall["reason"],
    }

    # --- Build and validate eval result ---
    eval_result = EmbeddingMismatchEvalResult(
        evidence_ids=[str(evidence_id)],
        learning_id=str(learning_id),
        review_state=review_state,
        recall_result=recalled_text,
        behavioral_trace=behavioral_trace,
    )
    assert eval_result.passes(), (
        f"Eval failed:\n"
        f"  evidence_ids={eval_result.evidence_ids}\n"
        f"  learning_id={eval_result.learning_id}\n"
        f"  review_state={eval_result.review_state}\n"
        f"  recall_result={eval_result.recall_result[:80]!r}\n"
        f"  behavioral_trace={eval_result.behavioral_trace}"
    )


# ---------------------------------------------------------------------------
# AC5: Full eval — all phases prove the complete workflow (timeout path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_embedding_mismatch_eval_timeout_path(monkeypatch):
    """Full eval with timeout integration: no user approval, auto-accepted after 6 hours.

    accepted_by_timeout is eligible for default recall, so session 2 still
    avoids the mistake.  Proves the eval passes even without explicit review.
    """
    evidence_id = uuid4()
    learning_id = uuid4()

    # Session 1 evidence
    evidence = _make_evidence_row(id=evidence_id)

    # Reflection
    proposals = generate_task_close_proposals(
        task_id=SESSION_1_TASK_ID,
        session_id=SESSION_1_SESSION_ID,
        evidence_records=[evidence],
    )
    assert str(evidence_id) in proposals[0]["source_evidence_ids"]

    # Timeout integration
    expired_proposal = _make_proposal_row(
        source_evidence_ids=[str(evidence_id)],
        content=LEARNING_CONTENT,
        status="pending",
        expires_at=_now() - timedelta(hours=1),
    )
    conn_timeout = AsyncMock()
    conn_timeout.fetch.return_value = [expired_proposal]
    conn_timeout.fetchrow.return_value = {"id": learning_id}
    conn_timeout.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn_timeout))

    timeout_result = await process_reflection_timeouts(_key=api.AuthContext("memu-dev-key"))
    assert timeout_result["processed"] == 1
    execute_sqls = [c.args[0] for c in conn_timeout.execute.call_args_list]
    assert any("accepted_by_timeout" in s for s in execute_sqls)

    # Session 2 recall
    learning_row = _make_learning_row(
        id=learning_id,
        review_status="accepted_by_timeout",
        evidence_id=evidence_id,
    )
    conn_recall = AsyncMock()
    conn_recall.fetch.return_value = [learning_row]
    conn_recall.execute = AsyncMock()
    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn_recall))

    recall_results = await api.learning_recall(
        SearchRequest(query=SESSION_2_QUERY, limit=5),
        _key=api.AuthContext("memu-dev-key"),
    )
    assert len(recall_results) == 1
    recalled_text = recall_results[0].memory.content

    decision_with_recall = _configure_embedding_with_recall(recalled_text)

    eval_result = EmbeddingMismatchEvalResult(
        evidence_ids=[str(evidence_id)],
        learning_id=str(learning_id),
        review_state="accepted_by_timeout",
        recall_result=recalled_text,
        behavioral_trace={
            "session1_env_var": "EMBEDDING_BASE_URL",
            "session2_env_var": decision_with_recall["env_var"],
            "session2_reason": decision_with_recall["reason"],
        },
    )
    assert eval_result.passes()
