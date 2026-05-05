"""Tests for the reflection pipeline (Issue #28).

Coverage:
- Scheduler cadence: should_run_task_close_reflection, should_run_idle_dream_reflection
- Proposal expiry helpers: compute_proposal_expiry, is_proposal_expired
- Task-close worker: generate_task_close_proposals (cap, ordering, empty content)
- Idle/dream worker: generate_idle_dream_proposals (cross-task pattern, stale candidate)
- Telegram formatter: format_telegram_compact_notice (structure, action commands, no raw evidence)
- TelegramNotifier: send_compact_notice (success, delivery failure, no send_fn)
- Queue lifecycle API: create, list, get, approve, deny, edit, inspect
- Timeout API: process-timeouts marks expired proposals as accepted_by_timeout
- Late feedback: edit on accepted proposal creates superseding record
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memu import api
from memu.api import (
    _row_to_proposal,
    create_reflection_proposal,
    list_reflection_proposals,
    get_reflection_proposal,
    take_reflection_action,
    process_reflection_timeouts,
)
from memu.models import (
    ReflectionAction,
    ReflectionActionRequest,
    ReflectionProposal,
    ReflectionProposalCreate,
    ReflectionSource,
)
from memu.reflection import (
    IDLE_DREAM_CADENCE_HOURS,
    REFLECTION_REVIEW_WINDOW_HOURS,
    TASK_CLOSE_MAX_PROPOSALS,
    TelegramNotifier,
    compute_proposal_expiry,
    format_telegram_compact_notice,
    generate_idle_dream_proposals,
    generate_task_close_proposals,
    is_proposal_expired,
    should_run_idle_dream_reflection,
    should_run_task_close_reflection,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_tenant_conn(conn):
    @asynccontextmanager
    async def _ctx(_auth):
        yield conn
    return _ctx


def _now():
    return datetime.now(timezone.utc)


def _make_proposal_row(
    *,
    proposal_id=None,
    status="pending",
    source="task_close",
    summary="some lesson",
    content="full lesson content",
    confidence=0.8,
    risk_flags=None,
    source_task_id="task-001",
    source_session_id="ses-001",
    source_evidence_ids=None,
    expires_at=None,
    telegram_message_id=None,
    memory_id=None,
    superseded_by=None,
    agent_id="reflection-worker",
    tenant_id="test-tenant",
):
    now = _now()
    return {
        "proposal_id": proposal_id or uuid4(),
        "tenant_id": tenant_id,
        "status": status,
        "source": source,
        "summary": summary,
        "content": content,
        "confidence": confidence,
        "risk_flags": risk_flags or [],
        "source_task_id": source_task_id,
        "source_session_id": source_session_id,
        "source_evidence_ids": source_evidence_ids or [],
        "expires_at": expires_at or (now + timedelta(hours=6)),
        "telegram_message_id": telegram_message_id,
        "memory_id": memory_id,
        "superseded_by": superseded_by,
        "agent_id": agent_id,
        "created_at": now,
        "updated_at": now,
    }


def _make_evidence_record(
    *,
    id=None,
    content="gateway ran task successfully",
    memory_type="user_action",
    agent_id="gw-1",
    confidence=0.9,
    metadata=None,
):
    return {
        "id": id or uuid4(),
        "content": content,
        "memory_type": memory_type,
        "agent_id": agent_id,
        "confidence": confidence,
        "metadata": metadata or {"task_id": "task-001", "event_type": "task_complete"},
    }


# ---------------------------------------------------------------------------
# 1. Scheduler cadence
# ---------------------------------------------------------------------------

def test_task_close_reflection_allowed_when_no_proposals():
    assert should_run_task_close_reflection(0) is True


def test_task_close_reflection_allowed_below_cap():
    assert should_run_task_close_reflection(2) is True


def test_task_close_reflection_blocked_at_cap():
    assert should_run_task_close_reflection(TASK_CLOSE_MAX_PROPOSALS) is False


def test_task_close_reflection_blocked_above_cap():
    assert should_run_task_close_reflection(10) is False


def test_idle_dream_runs_when_never_ran():
    assert should_run_idle_dream_reflection(None) is True


def test_idle_dream_blocked_within_cadence_window():
    recent = _now() - timedelta(hours=IDLE_DREAM_CADENCE_HOURS - 1)
    assert should_run_idle_dream_reflection(recent) is False


def test_idle_dream_runs_after_cadence_window():
    old = _now() - timedelta(hours=IDLE_DREAM_CADENCE_HOURS + 1)
    assert should_run_idle_dream_reflection(old) is True


def test_idle_dream_runs_exactly_at_cadence_boundary():
    boundary = _now() - timedelta(hours=IDLE_DREAM_CADENCE_HOURS)
    assert should_run_idle_dream_reflection(boundary) is True


def test_idle_dream_uses_provided_now():
    last_ran = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    now_before = datetime(2026, 1, 1, 3, 59, 59, tzinfo=timezone.utc)
    now_after = datetime(2026, 1, 1, 4, 0, 0, tzinfo=timezone.utc)
    assert should_run_idle_dream_reflection(last_ran, now=now_before) is False
    assert should_run_idle_dream_reflection(last_ran, now=now_after) is True


# ---------------------------------------------------------------------------
# 2. Proposal expiry helpers
# ---------------------------------------------------------------------------

def test_compute_proposal_expiry_is_six_hours():
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    expected = datetime(2026, 5, 1, 18, 0, 0, tzinfo=timezone.utc)
    assert compute_proposal_expiry(base) == expected


def test_compute_proposal_expiry_uses_now_when_no_arg():
    before = _now()
    expiry = compute_proposal_expiry()
    after = _now()
    assert before + timedelta(hours=REFLECTION_REVIEW_WINDOW_HOURS) <= expiry
    assert expiry <= after + timedelta(hours=REFLECTION_REVIEW_WINDOW_HOURS)


def test_is_proposal_expired_returns_true_for_past():
    past = _now() - timedelta(seconds=1)
    assert is_proposal_expired(past) is True


def test_is_proposal_expired_returns_false_for_future():
    future = _now() + timedelta(hours=1)
    assert is_proposal_expired(future) is False


def test_is_proposal_expired_with_explicit_now():
    expires = datetime(2026, 5, 1, 18, 0, 0, tzinfo=timezone.utc)
    assert is_proposal_expired(expires, now=datetime(2026, 5, 1, 18, 0, 1, tzinfo=timezone.utc)) is True
    assert is_proposal_expired(expires, now=datetime(2026, 5, 1, 17, 59, 59, tzinfo=timezone.utc)) is False


# ---------------------------------------------------------------------------
# 3. Task-close proposal generator
# ---------------------------------------------------------------------------

def test_task_close_emits_at_most_three_proposals():
    records = [_make_evidence_record(content=f"evidence {i}") for i in range(10)]
    proposals = generate_task_close_proposals("t-1", "s-1", records)
    assert len(proposals) <= TASK_CLOSE_MAX_PROPOSALS


def test_task_close_respects_existing_count():
    records = [_make_evidence_record(content=f"evidence {i}") for i in range(5)]
    # 2 already exist → at most 1 more
    proposals = generate_task_close_proposals("t-1", "s-1", records, existing_proposal_count=2)
    assert len(proposals) <= 1


def test_task_close_returns_empty_at_cap():
    records = [_make_evidence_record(content="evidence")]
    proposals = generate_task_close_proposals("t-1", "s-1", records, existing_proposal_count=3)
    assert proposals == []


def test_task_close_skips_empty_content():
    records = [_make_evidence_record(content="")]
    proposals = generate_task_close_proposals("t-1", "s-1", records)
    assert proposals == []


def test_task_close_prioritises_failures():
    failure = _make_evidence_record(content="failure happened", memory_type="failure")
    success = _make_evidence_record(content="success happened", memory_type="user_action")
    proposals = generate_task_close_proposals("t-1", "s-1", [success, failure])
    # Failure should appear first
    assert proposals[0]["content"] == "failure happened"


def test_task_close_proposal_has_correct_source_fields():
    record = _make_evidence_record(content="completed work")
    proposals = generate_task_close_proposals("task-42", "session-7", [record])
    assert len(proposals) == 1
    p = proposals[0]
    assert p["source"] == "task_close"
    assert p["source_task_id"] == "task-42"
    assert p["source_session_id"] == "session-7"
    assert str(record["id"]) in p["source_evidence_ids"]


def test_task_close_truncates_summary_to_200_chars():
    long_content = "x" * 300
    record = _make_evidence_record(content=long_content)
    proposals = generate_task_close_proposals("t-1", None, [record])
    assert len(proposals[0]["summary"]) <= 203  # 200 chars + "..."


# ---------------------------------------------------------------------------
# 4. Idle/dream proposal generator
# ---------------------------------------------------------------------------

def test_idle_dream_detects_cross_task_repeated_failure():
    failure_1 = _make_evidence_record(
        content="embedding config mismatch",
        memory_type="failure",
        metadata={"task_id": "task-A"},
    )
    failure_2 = _make_evidence_record(
        content="embedding config mismatch",
        memory_type="failure",
        metadata={"task_id": "task-B"},
    )
    proposals = generate_idle_dream_proposals([failure_1, failure_2], [])
    assert len(proposals) == 1
    assert proposals[0]["source"] == "idle_dream"
    assert "repeated_failure" in proposals[0]["risk_flags"]
    assert "task-A" in proposals[0]["content"] or "task-B" in proposals[0]["content"]


def test_idle_dream_no_proposal_for_single_task_failure():
    failure = _make_evidence_record(
        content="isolated failure",
        memory_type="failure",
        metadata={"task_id": "task-X"},
    )
    proposals = generate_idle_dream_proposals([failure], [])
    assert proposals == []


def test_idle_dream_flags_stale_learning_candidates():
    stale = {"id": uuid4(), "content": "old lesson about X", "memory_type": "lesson"}
    proposals = generate_idle_dream_proposals([], [stale])
    assert len(proposals) == 1
    assert proposals[0]["source"] == "idle_dream"
    assert "stale_learning" in proposals[0]["risk_flags"]


def test_idle_dream_caps_stale_candidates_at_two():
    stale_records = [
        {"id": uuid4(), "content": f"old lesson {i}", "memory_type": "lesson"}
        for i in range(5)
    ]
    proposals = generate_idle_dream_proposals([], stale_records)
    stale_proposals = [p for p in proposals if "stale_learning" in p["risk_flags"]]
    assert len(stale_proposals) <= 2


def test_idle_dream_skips_empty_content():
    stale = {"id": uuid4(), "content": "", "memory_type": "lesson"}
    proposals = generate_idle_dream_proposals([], [stale])
    assert proposals == []


# ---------------------------------------------------------------------------
# 5. Telegram compact notice formatter
# ---------------------------------------------------------------------------

def test_format_telegram_notice_contains_action_commands():
    pid = uuid4()
    expires = datetime(2026, 5, 1, 18, 0, 0, tzinfo=timezone.utc)
    notice = format_telegram_compact_notice(
        proposal_id=pid,
        summary="learned something",
        confidence=0.85,
        risk_flags=["failure_evidence"],
        source_task_id="task-99",
        expires_at=expires,
        source="task_close",
    )
    assert f"/approve_{pid}" in notice
    assert f"/deny_{pid}" in notice
    assert f"/edit_{pid}" in notice
    assert f"/inspect_{pid}" in notice


def test_format_telegram_notice_does_not_include_raw_evidence():
    pid = uuid4()
    expires = _now() + timedelta(hours=6)
    raw_evidence = "INTERNAL_RAW_TOOL_TRACE_DATA"
    notice = format_telegram_compact_notice(
        proposal_id=pid,
        summary="short summary only",
        confidence=0.7,
        risk_flags=[],
        source_task_id=None,
        expires_at=expires,
        source="idle_dream",
    )
    assert raw_evidence not in notice


def test_format_telegram_notice_includes_confidence_and_risk():
    pid = uuid4()
    expires = _now() + timedelta(hours=6)
    notice = format_telegram_compact_notice(
        proposal_id=pid,
        summary="lesson",
        confidence=0.75,
        risk_flags=["high_risk"],
        source_task_id="task-5",
        expires_at=expires,
        source="task_close",
    )
    assert "75%" in notice
    assert "high_risk" in notice


def test_format_telegram_notice_includes_expiry():
    pid = uuid4()
    expires = datetime(2026, 5, 1, 18, 30, 0, tzinfo=timezone.utc)
    notice = format_telegram_compact_notice(
        proposal_id=pid,
        summary="lesson",
        confidence=0.9,
        risk_flags=[],
        source_task_id=None,
        expires_at=expires,
        source="idle_dream",
    )
    assert "2026-05-01" in notice
    assert "18:30" in notice


def test_format_telegram_notice_uses_idle_dream_source_label_when_no_task():
    pid = uuid4()
    expires = _now() + timedelta(hours=6)
    notice = format_telegram_compact_notice(
        proposal_id=pid,
        summary="cross-task pattern",
        confidence=0.6,
        risk_flags=[],
        source_task_id=None,
        expires_at=expires,
        source="idle_dream",
    )
    assert "[idle_dream]" in notice


# ---------------------------------------------------------------------------
# 6. TelegramNotifier adapter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telegram_notifier_calls_send_fn():
    pid = uuid4()
    expires = _now() + timedelta(hours=6)
    captured = []

    async def fake_send(msg: str):
        captured.append(msg)
        return "tg-msg-id-42"

    notifier = TelegramNotifier(send_fn=fake_send)
    result = await notifier.send_compact_notice(
        proposal_id=pid,
        summary="test lesson",
        confidence=0.8,
        risk_flags=[],
        source_task_id=None,
        expires_at=expires,
        source="task_close",
    )
    assert result == "tg-msg-id-42"
    assert len(captured) == 1
    assert f"/approve_{pid}" in captured[0]


@pytest.mark.asyncio
async def test_telegram_notifier_returns_none_on_delivery_failure():
    pid = uuid4()
    expires = _now() + timedelta(hours=6)

    async def failing_send(msg: str):
        raise ConnectionError("Telegram unreachable")

    notifier = TelegramNotifier(send_fn=failing_send)
    result = await notifier.send_compact_notice(
        proposal_id=pid,
        summary="test lesson",
        confidence=0.8,
        risk_flags=[],
        source_task_id=None,
        expires_at=expires,
        source="task_close",
    )
    assert result is None


@pytest.mark.asyncio
async def test_telegram_notifier_without_send_fn_returns_none():
    pid = uuid4()
    expires = _now() + timedelta(hours=6)
    notifier = TelegramNotifier()
    result = await notifier.send_compact_notice(
        proposal_id=pid,
        summary="lesson",
        confidence=0.7,
        risk_flags=[],
        source_task_id="task-1",
        expires_at=expires,
        source="task_close",
    )
    assert result is None


@pytest.mark.asyncio
async def test_telegram_notifier_delivery_failure_does_not_propagate():
    """Delivery failure must never raise — the proposal lifecycle must not be blocked."""
    pid = uuid4()
    expires = _now() + timedelta(hours=6)

    async def always_fails(msg: str):
        raise RuntimeError("critical telegram outage")

    notifier = TelegramNotifier(send_fn=always_fails)
    try:
        result = await notifier.send_compact_notice(
            proposal_id=pid,
            summary="lesson",
            confidence=0.7,
            risk_flags=[],
            source_task_id=None,
            expires_at=expires,
            source="idle_dream",
        )
    except Exception:
        pytest.fail("TelegramNotifier must not propagate delivery exceptions")
    assert result is None


# ---------------------------------------------------------------------------
# 7. _row_to_proposal helper
# ---------------------------------------------------------------------------

def test_row_to_proposal_basic():
    row = _make_proposal_row()
    proposal = _row_to_proposal(row)
    assert isinstance(proposal, ReflectionProposal)
    assert proposal.status == "pending"
    assert proposal.source == "task_close"
    assert proposal.confidence == 0.8


def test_row_to_proposal_deserialises_json_risk_flags():
    row = _make_proposal_row(risk_flags=["failure_evidence", "high_risk"])
    proposal = _row_to_proposal(row)
    assert "failure_evidence" in proposal.risk_flags
    assert "high_risk" in proposal.risk_flags


def test_row_to_proposal_handles_string_json_fields():
    row = _make_proposal_row()
    row["risk_flags"] = '["stale_learning"]'
    row["source_evidence_ids"] = '["ev-001"]'
    proposal = _row_to_proposal(row)
    assert "stale_learning" in proposal.risk_flags
    assert "ev-001" in proposal.source_evidence_ids


# ---------------------------------------------------------------------------
# 8. Queue lifecycle API (mocked DB)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_reflection_proposal_inserts_row(monkeypatch):
    expected_row = _make_proposal_row()
    conn = AsyncMock()
    conn.fetchrow.return_value = expected_row
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ReflectionProposalCreate(
        source=ReflectionSource.task_close,
        summary="learned something important",
        content="full content of the lesson",
        confidence=0.8,
        agent_id="gw-1",
    )
    result = await create_reflection_proposal(req, _key=api.AuthContext("memu-dev-key"))

    assert isinstance(result, ReflectionProposal)
    assert conn.fetchrow.called
    insert_sql = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO reflection_proposals" in insert_sql
    assert "pending" in insert_sql


@pytest.mark.asyncio
async def test_list_reflection_proposals_filters_by_pending(monkeypatch):
    conn = AsyncMock()
    conn.fetch.return_value = [_make_proposal_row()]
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    results = await list_reflection_proposals(_key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "status = 'pending'" in fetch_sql
    assert len(results) == 1


@pytest.mark.asyncio
async def test_list_reflection_proposals_accepts_status_filter(monkeypatch):
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    await list_reflection_proposals(status="accepted", _key=api.AuthContext("memu-dev-key"))

    fetch_sql = conn.fetch.await_args.args[0]
    assert "status = " in fetch_sql
    # 'accepted' is passed as a query param
    assert "accepted" in conn.fetch.await_args.args


@pytest.mark.asyncio
async def test_get_reflection_proposal_returns_proposal(monkeypatch):
    pid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = _make_proposal_row(proposal_id=pid)
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await get_reflection_proposal(pid, _key=api.AuthContext("memu-dev-key"))
    assert result.proposal_id == pid


@pytest.mark.asyncio
async def test_get_reflection_proposal_raises_404_when_missing(monkeypatch):
    from fastapi import HTTPException
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    with pytest.raises(HTTPException) as exc_info:
        await get_reflection_proposal(uuid4(), _key=api.AuthContext("memu-dev-key"))
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# 9. Approve action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_pending_proposal_writes_memory(monkeypatch):
    pid = uuid4()
    mid = uuid4()
    proposal_row = _make_proposal_row(proposal_id=pid)

    conn = AsyncMock()
    # fetchrow: first call returns proposal, second (for memory insert) returns {"id": mid}
    conn.fetchrow.side_effect = [proposal_row, {"id": mid}]
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    req = ReflectionActionRequest(actor="user-1", decision=ReflectionAction.approve)
    result = await take_reflection_action(pid, req, _key=api.AuthContext("memu-dev-key"))

    assert result.decision == "approve"
    assert result.status == "accepted"
    assert result.memory_id == mid

    # Verify INSERT into memories was called with review_status='accepted'
    memory_insert = conn.fetchrow.call_args_list[1]
    assert "INSERT INTO memories" in memory_insert.args[0]
    assert "review_status" in memory_insert.args[0]
    assert "accepted" in memory_insert.args


@pytest.mark.asyncio
async def test_approve_updates_proposal_status(monkeypatch):
    pid = uuid4()
    mid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_make_proposal_row(proposal_id=pid), {"id": mid}]
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    await take_reflection_action(
        pid,
        ReflectionActionRequest(actor="u", decision=ReflectionAction.approve),
        _key=api.AuthContext("memu-dev-key"),
    )

    update_calls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("UPDATE reflection_proposals" in s and "accepted" in s for s in update_calls)


@pytest.mark.asyncio
async def test_approve_already_accepted_raises_409(monkeypatch):
    from fastapi import HTTPException
    pid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = _make_proposal_row(proposal_id=pid, status="accepted")
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    with pytest.raises(HTTPException) as exc_info:
        await take_reflection_action(
            pid,
            ReflectionActionRequest(actor="u", decision=ReflectionAction.approve),
            _key=api.AuthContext("memu-dev-key"),
        )
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# 10. Deny action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deny_pending_proposal_rejects_without_memory_write(monkeypatch):
    pid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = _make_proposal_row(proposal_id=pid)
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await take_reflection_action(
        pid,
        ReflectionActionRequest(actor="user-1", decision=ReflectionAction.deny),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.decision == "deny"
    assert result.status == "rejected"
    assert result.memory_id is None

    # No INSERT into memories
    execute_sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert not any("INSERT INTO memories" in s for s in execute_sqls)


@pytest.mark.asyncio
async def test_deny_non_pending_raises_409(monkeypatch):
    from fastapi import HTTPException
    pid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = _make_proposal_row(proposal_id=pid, status="rejected")
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    with pytest.raises(HTTPException) as exc_info:
        await take_reflection_action(
            pid,
            ReflectionActionRequest(actor="u", decision=ReflectionAction.deny),
            _key=api.AuthContext("memu-dev-key"),
        )
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# 11. Edit action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_edit_pending_proposal_accepts_with_edited_content(monkeypatch):
    pid = uuid4()
    mid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_make_proposal_row(proposal_id=pid), {"id": mid}]
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await take_reflection_action(
        pid,
        ReflectionActionRequest(
            actor="user-1",
            decision=ReflectionAction.edit,
            edited_content="improved lesson content",
        ),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.decision == "edit"
    assert result.status == "accepted"
    assert result.memory_id == mid

    # Verify the edited content was passed to the memory INSERT
    memory_insert = conn.fetchrow.call_args_list[1]
    assert "improved lesson content" in memory_insert.args


@pytest.mark.asyncio
async def test_edit_without_edited_content_raises_422(monkeypatch):
    from fastapi import HTTPException
    pid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = _make_proposal_row(proposal_id=pid)
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    with pytest.raises(HTTPException) as exc_info:
        await take_reflection_action(
            pid,
            ReflectionActionRequest(actor="u", decision=ReflectionAction.edit),
            _key=api.AuthContext("memu-dev-key"),
        )
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# 12. Late feedback supersession (edit on accepted proposal)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_late_feedback_edit_on_accepted_creates_superseding_memory(monkeypatch):
    """Edit on an already-accepted proposal creates superseding memory without mutating history."""
    pid = uuid4()
    original_mid = uuid4()
    new_mid = uuid4()

    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        _make_proposal_row(proposal_id=pid, status="accepted", memory_id=original_mid),
        {"id": new_mid},
    ]
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await take_reflection_action(
        pid,
        ReflectionActionRequest(
            actor="user-1",
            decision=ReflectionAction.edit,
            edited_content="corrected lesson after timeout",
        ),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.decision == "edit"
    assert result.status == "superseded"
    assert result.memory_id == new_mid
    assert result.supersedes_id == original_mid

    # Verify the old memory was marked invalid (UPDATE memories ... SET valid_to)
    execute_sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("UPDATE memories" in s and "valid_to" in s for s in execute_sqls)


@pytest.mark.asyncio
async def test_late_feedback_edit_on_accepted_by_timeout(monkeypatch):
    """Late feedback also works on proposals that were accepted_by_timeout."""
    pid = uuid4()
    original_mid = uuid4()
    new_mid = uuid4()

    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        _make_proposal_row(
            proposal_id=pid,
            status="accepted_by_timeout",
            memory_id=original_mid,
        ),
        {"id": new_mid},
    ]
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await take_reflection_action(
        pid,
        ReflectionActionRequest(
            actor="user-1",
            decision=ReflectionAction.edit,
            edited_content="revised content",
        ),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.decision == "edit"
    assert result.status == "superseded"
    assert result.memory_id == new_mid


# ---------------------------------------------------------------------------
# 13. Inspect action
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inspect_action_does_not_change_status(monkeypatch):
    pid = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = _make_proposal_row(proposal_id=pid, status="pending")
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await take_reflection_action(
        pid,
        ReflectionActionRequest(actor="user-1", decision=ReflectionAction.inspect),
        _key=api.AuthContext("memu-dev-key"),
    )

    assert result.decision == "inspect"
    assert result.status == "pending"

    # No UPDATE to proposal status
    execute_sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert not any("UPDATE reflection_proposals" in s for s in execute_sqls)


# ---------------------------------------------------------------------------
# 14. Timeout processing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_timeouts_marks_expired_as_accepted_by_timeout(monkeypatch):
    pid = uuid4()
    mid = uuid4()
    expired_row = _make_proposal_row(
        proposal_id=pid,
        status="pending",
        expires_at=_now() - timedelta(hours=1),
    )

    conn = AsyncMock()
    # fetch returns the expired proposal; fetchrow returns new memory id
    conn.fetch.return_value = [expired_row]
    conn.fetchrow.return_value = {"id": mid}
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await process_reflection_timeouts(_key=api.AuthContext("memu-dev-key"))

    assert result["processed"] == 1

    # Verify INSERT into memories with accepted_by_timeout
    memory_insert = conn.fetchrow.call_args
    assert "INSERT INTO memories" in memory_insert.args[0]
    assert "review_status" in memory_insert.args[0]
    assert "accepted_by_timeout" in memory_insert.args

    # Verify UPDATE reflection_proposals to accepted_by_timeout
    execute_sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any(
        "UPDATE reflection_proposals" in s and "accepted_by_timeout" in s
        for s in execute_sqls
    )


@pytest.mark.asyncio
async def test_process_timeouts_skips_non_expired_proposals(monkeypatch):
    conn = AsyncMock()
    conn.fetch.return_value = []  # No expired proposals
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    result = await process_reflection_timeouts(_key=api.AuthContext("memu-dev-key"))

    assert result["processed"] == 0
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_process_timeouts_records_audit_action(monkeypatch):
    pid = uuid4()
    mid = uuid4()
    expired_row = _make_proposal_row(
        proposal_id=pid,
        status="pending",
        expires_at=_now() - timedelta(hours=1),
    )

    conn = AsyncMock()
    conn.fetch.return_value = [expired_row]
    conn.fetchrow.return_value = {"id": mid}
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    await process_reflection_timeouts(_key=api.AuthContext("memu-dev-key"))

    execute_sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any(
        "INSERT INTO reflection_actions" in s and "timeout_accept" in s
        for s in execute_sqls
    )


@pytest.mark.asyncio
async def test_process_timeouts_timeout_memory_is_eligible_for_recall(monkeypatch):
    """Memory written by timeout must have review_status=accepted_by_timeout so it appears in recall."""
    pid = uuid4()
    mid = uuid4()
    expired_row = _make_proposal_row(
        proposal_id=pid,
        status="pending",
        expires_at=_now() - timedelta(hours=1),
    )

    conn = AsyncMock()
    conn.fetch.return_value = [expired_row]
    conn.fetchrow.return_value = {"id": mid}
    conn.execute = AsyncMock()
    monkeypatch.setattr(api, "_tenant_conn", _fake_tenant_conn(conn))

    await process_reflection_timeouts(_key=api.AuthContext("memu-dev-key"))

    # The INSERT must include the review_status column
    memory_insert = conn.fetchrow.call_args
    assert "review_status" in memory_insert.args[0]
    # And the value 'accepted_by_timeout' must be passed as a parameter
    assert "accepted_by_timeout" in memory_insert.args


# ---------------------------------------------------------------------------
# 15. ReflectionProposalCreate model validation
# ---------------------------------------------------------------------------

def test_reflection_proposal_create_defaults():
    req = ReflectionProposalCreate(
        source=ReflectionSource.idle_dream,
        summary="summary",
        content="content",
        agent_id="agent-1",
    )
    assert req.confidence == 0.7
    assert req.risk_flags == []
    assert req.source_evidence_ids == []
    assert req.source_task_id is None


def test_reflection_proposal_create_confidence_bounds():
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        ReflectionProposalCreate(
            source=ReflectionSource.task_close,
            summary="s",
            content="c",
            agent_id="a",
            confidence=1.5,
        )
