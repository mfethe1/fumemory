"""Tests for OpenClaw hook behavior, criticality routing, and waiver evidence.

Coverage:
- log_action posts to canonical endpoint /api/v1/memu/add (not /memories/async)
- log_action with COMPLETION_PROOF criticality raises CompletionProofWriteError on failure
- log_action with TELEMETRY criticality retries TELEMETRY_MAX_RETRIES times then degrades
- log_search uses TELEMETRY criticality and posts to canonical endpoint
- write_waiver posts waiver evidence to canonical endpoint (COMPLETION_PROOF)
- write_waiver failure raises CompletionProofWriteError
- Metadata records criticality value for routing auditing
- Task/session context is forwarded in metadata
- Idempotency key is forwarded to canonical endpoint
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from memu.openclaw_hooks import (
    CANONICAL_WRITE_ENDPOINT,
    MEMU_API_URL,
    TELEMETRY_MAX_RETRIES,
    CompletionProofWriteError,
    WriteCriticality,
    log_action,
    log_search,
    write_waiver,
)

_CANONICAL_URL = f"{MEMU_API_URL}{CANONICAL_WRITE_ENDPOINT}"


def _ok_response(memory_id: str = "abc-123") -> dict:
    return {"ok": True, "id": memory_id, "message": "Memory stored"}


# ---------------------------------------------------------------------------
# 1. Canonical endpoint — log_action uses /api/v1/memu/add
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_action_posts_to_canonical_endpoint():
    """log_action must write to /api/v1/memu/add, not /memories/async."""
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_action("gw-1", "task_started", {"task": "t1"})

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["memory_type"] == "user_action"
    assert body["memory_kind"] == "evidence"


@pytest.mark.asyncio
async def test_log_action_does_not_post_to_memories_async():
    """log_action must not call /memories/async — that path is not canonical."""
    with respx.mock(assert_all_called=False) as mock:
        async_route = mock.post(f"{MEMU_API_URL}/memories/async").mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        canonical_route = mock.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_action("gw-1", "task_started", {})

    assert not async_route.called
    assert canonical_route.called


# ---------------------------------------------------------------------------
# 2. Canonical endpoint — log_search uses /api/v1/memu/add
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_search_posts_to_canonical_endpoint():
    """log_search must write to /api/v1/memu/add, not /memories/async."""
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_search("gw-1", "how to run tests", "pytest runs unit tests")

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["memory_type"] == "external"
    assert body["memory_kind"] == "evidence"


# ---------------------------------------------------------------------------
# 3. COMPLETION_PROOF failures raise CompletionProofWriteError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completion_proof_http_failure_raises():
    """A completion-proof write that gets an HTTP error must raise."""
    with respx.mock:
        respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(503, json={"detail": "Service Unavailable"})
        )
        with pytest.raises(CompletionProofWriteError) as exc_info:
            await log_action(
                "gw-1",
                "task_complete",
                {"task": "t1"},
                criticality=WriteCriticality.COMPLETION_PROOF,
            )

    assert exc_info.value.action == "task_complete"
    assert isinstance(exc_info.value.cause, Exception)


@pytest.mark.asyncio
async def test_completion_proof_network_error_raises():
    """Network errors on completion-proof writes must also raise."""
    with respx.mock:
        respx.post(_CANONICAL_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with pytest.raises(CompletionProofWriteError):
            await log_action(
                "gw-1",
                "readiness_check",
                {},
                criticality=WriteCriticality.COMPLETION_PROOF,
            )


@pytest.mark.asyncio
async def test_completion_proof_success_returns_result():
    """A successful completion-proof write returns the API response."""
    with respx.mock:
        respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response("mem-001"))
        )
        result = await log_action(
            "gw-1",
            "task_complete",
            {},
            criticality=WriteCriticality.COMPLETION_PROOF,
        )

    assert result is not None
    assert result["id"] == "mem-001"


# ---------------------------------------------------------------------------
# 4. TELEMETRY failures retry then degrade without raising
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_telemetry_failure_retries_exactly_max_times():
    """Telemetry write must retry exactly TELEMETRY_MAX_RETRIES times on failure."""
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(500, json={"detail": "Internal Server Error"})
        )
        result = await log_action(
            "gw-1",
            "tool_called",
            {"tool": "web_search"},
            criticality=WriteCriticality.TELEMETRY,
        )

    assert result is None
    assert route.call_count == TELEMETRY_MAX_RETRIES


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_raise():
    """Telemetry write degradation must not raise an exception."""
    with respx.mock:
        respx.post(_CANONICAL_URL).mock(return_value=httpx.Response(503))
        result = await log_action(
            "gw-1",
            "low_risk_event",
            {},
            criticality=WriteCriticality.TELEMETRY,
        )

    assert result is None


@pytest.mark.asyncio
async def test_telemetry_succeeds_on_second_attempt():
    """Telemetry write that succeeds on retry returns the result."""
    responses = [
        httpx.Response(500),
        httpx.Response(200, json=_ok_response("mem-456")),
    ]
    with respx.mock:
        respx.post(_CANONICAL_URL).mock(side_effect=responses)
        result = await log_action(
            "gw-1",
            "tool_called",
            {},
            criticality=WriteCriticality.TELEMETRY,
        )

    assert result is not None
    assert result["id"] == "mem-456"


@pytest.mark.asyncio
async def test_log_search_degrades_on_failure():
    """log_search uses telemetry criticality — failures degrade without raising."""
    with respx.mock:
        respx.post(_CANONICAL_URL).mock(return_value=httpx.Response(503))
        result = await log_search("gw-1", "some query", "results")

    assert result is None


# ---------------------------------------------------------------------------
# 5. Waiver evidence storage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_waiver_posts_canonical_waiver_evidence():
    """write_waiver must post waiver evidence to /api/v1/memu/add."""
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response("waiver-789"))
        )
        result = await write_waiver(
            agent_id="gw-1",
            action="task_complete",
            reason="Database unavailable; manually verified completion",
            operator_id="operator-mfeth",
            task_id="task-abc",
        )

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["memory_type"] == "user_action"
    assert body["memory_kind"] == "evidence"
    assert body["metadata"]["waiver"] is True
    assert body["metadata"]["waiver_for_action"] == "task_complete"
    assert body["metadata"]["operator_id"] == "operator-mfeth"
    assert body["metadata"]["task_id"] == "task-abc"
    assert result["id"] == "waiver-789"


@pytest.mark.asyncio
async def test_write_waiver_failure_raises_completion_proof_error():
    """write_waiver failure must raise CompletionProofWriteError — waivers are completion proof."""
    with respx.mock:
        respx.post(_CANONICAL_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(CompletionProofWriteError) as exc_info:
            await write_waiver("gw-1", "task_complete", "reason", "operator-1")

    assert "waiver:task_complete" in exc_info.value.action


# ---------------------------------------------------------------------------
# 6. Criticality routing: metadata records criticality for auditing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completion_proof_metadata_records_criticality():
    """Canonical writes must record criticality in metadata."""
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_action(
            "gw-1",
            "task_complete",
            {},
            criticality=WriteCriticality.COMPLETION_PROOF,
        )

    body = json.loads(route.calls.last.request.content)
    assert body["metadata"]["criticality"] == WriteCriticality.COMPLETION_PROOF.value


@pytest.mark.asyncio
async def test_telemetry_metadata_records_criticality():
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_action(
            "gw-1",
            "low_risk",
            {},
            criticality=WriteCriticality.TELEMETRY,
        )

    body = json.loads(route.calls.last.request.content)
    assert body["metadata"]["criticality"] == WriteCriticality.TELEMETRY.value


@pytest.mark.asyncio
async def test_log_search_metadata_records_telemetry_criticality():
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_search("gw-1", "test query", "results")

    body = json.loads(route.calls.last.request.content)
    assert body["metadata"]["criticality"] == WriteCriticality.TELEMETRY.value


# ---------------------------------------------------------------------------
# 7. Task/session context forwarded in metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_log_action_includes_task_and_session_in_metadata():
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_action(
            "gw-1",
            "task_started",
            {},
            task_id="task-001",
            session_id="session-999",
        )

    body = json.loads(route.calls.last.request.content)
    assert body["metadata"]["task_id"] == "task-001"
    assert body["metadata"]["session_id"] == "session-999"


@pytest.mark.asyncio
async def test_log_action_omits_task_session_when_not_provided():
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_action("gw-1", "task_started", {})

    body = json.loads(route.calls.last.request.content)
    assert "task_id" not in body["metadata"]
    assert "session_id" not in body["metadata"]


# ---------------------------------------------------------------------------
# 8. Idempotency key forwarded to canonical endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotency_key_is_forwarded():
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_action(
            "gw-1",
            "task_complete",
            {},
            idempotency_key="task-001:complete",
        )

    body = json.loads(route.calls.last.request.content)
    assert body["idempotency_key"] == "task-001:complete"


@pytest.mark.asyncio
async def test_no_idempotency_key_when_not_provided():
    with respx.mock:
        route = respx.post(_CANONICAL_URL).mock(
            return_value=httpx.Response(200, json=_ok_response())
        )
        await log_action("gw-1", "task_complete", {})

    body = json.loads(route.calls.last.request.content)
    assert "idempotency_key" not in body


# ---------------------------------------------------------------------------
# 9. WriteCriticality enum values match domain language
# ---------------------------------------------------------------------------

def test_write_criticality_values():
    assert WriteCriticality.COMPLETION_PROOF.value == "completion_proof"
    assert WriteCriticality.TELEMETRY.value == "telemetry"


def test_completion_proof_write_error_carries_action_and_cause():
    cause = ValueError("db down")
    err = CompletionProofWriteError(action="readiness_check", cause=cause)
    assert err.action == "readiness_check"
    assert err.cause is cause
    assert "readiness_check" in str(err)
