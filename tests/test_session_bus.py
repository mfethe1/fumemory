"""Tests for session_bus — session-level event publishing (M1).

Covers:
- SessionEventBus.task_completed() publishes TASK_COMPLETED event
- SessionEventBus.task_completed() with error publishes TASK_FAILED event
- SessionEventBus.memory_write() publishes CHECKPOINT_SAVED event
- SessionEventBus.decision() publishes AUDIT_ACCEPTED event
- Events are valid SwarmEvent instances (all required fields present)
- SESSION_BUS_EMIT=false suppresses publishing without error
- fire-and-forget module-level hooks work end-to-end
- Metrics counters increment correctly
- UUID coercion handles None / str / UUID inputs
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from memu.session_bus import (
    SessionEventBus,
    hook_session_decision,
    hook_session_memory_write,
    hook_session_task_completed,
)
from memu.swarm_models import EventType, SwarmEvent


# ---------------------------------------------------------------------------
# Fake NATS infrastructure
# ---------------------------------------------------------------------------

class _FakeAck:
    seq = 42


class _FakeJetStream:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> _FakeAck:
        self.published.append((subject, data))
        return _FakeAck()


class _FakeManager:
    def __init__(self) -> None:
        self.js = _FakeJetStream()
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    @property
    def jetstream(self) -> _FakeJetStream:
        return self.js


def _make_bus(agent_id: str = "test-agent", emit: bool = True) -> tuple[SessionEventBus, _FakeJetStream]:
    mgr = _FakeManager()
    bus = SessionEventBus(agent_id=agent_id, cluster_manager=mgr, emit=emit)
    return bus, mgr.js


def _parse_event(raw: bytes) -> SwarmEvent:
    return SwarmEvent.model_validate(json.loads(raw.decode()))


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestTaskCompleted:
    @pytest.mark.asyncio
    async def test_publishes_task_completed(self):
        bus, js = _make_bus()
        await bus.connect()
        tid = uuid4()
        ok = await bus.task_completed(task_id=tid, result={"score": 0.9}, tokens_used=200, duration_ms=500)

        assert ok is True
        assert len(js.published) == 1
        subject, raw = js.published[0]
        assert subject == "AGENT_EVENTS"

        event = _parse_event(raw)
        assert event.event_type == EventType.TASK_COMPLETED
        assert event.source_gateway == "test-agent"
        assert event.task_id == tid
        assert event.payload["tokens_used"] == 200
        assert event.payload["duration_ms"] == 500
        assert event.payload["result"] == {"score": 0.9}

    @pytest.mark.asyncio
    async def test_publishes_task_failed_when_error(self):
        bus, js = _make_bus()
        await bus.connect()
        ok = await bus.task_completed(error="something went wrong")

        assert ok is True
        event = _parse_event(js.published[0][1])
        assert event.event_type == EventType.TASK_FAILED
        assert event.payload["error"] == "something went wrong"

    @pytest.mark.asyncio
    async def test_mints_uuid_when_task_id_none(self):
        bus, js = _make_bus()
        await bus.connect()
        await bus.task_completed()

        event = _parse_event(js.published[0][1])
        # Should have a valid UUID even without explicit task_id
        assert isinstance(event.task_id, UUID)

    @pytest.mark.asyncio
    async def test_accepts_string_task_id(self):
        bus, js = _make_bus()
        await bus.connect()
        tid_str = str(uuid4())
        await bus.task_completed(task_id=tid_str)

        event = _parse_event(js.published[0][1])
        assert str(event.task_id) == tid_str


class TestMemoryWrite:
    @pytest.mark.asyncio
    async def test_publishes_checkpoint_saved(self):
        bus, js = _make_bus()
        await bus.connect()
        ok = await bus.memory_write("stored the plan for BuildBid", namespace="plan")

        assert ok is True
        event = _parse_event(js.published[0][1])
        assert event.event_type == EventType.CHECKPOINT_SAVED
        assert event.payload["content_preview"] == "stored the plan for BuildBid"
        assert event.payload["namespace"] == "plan"
        assert event.payload["agent_id"] == "test-agent"

    @pytest.mark.asyncio
    async def test_truncates_long_content(self):
        bus, js = _make_bus()
        await bus.connect()
        long_content = "x" * 1000
        await bus.memory_write(long_content)

        event = _parse_event(js.published[0][1])
        assert len(event.payload["content_preview"]) <= 256

    @pytest.mark.asyncio
    async def test_metadata_merged(self):
        bus, js = _make_bus()
        await bus.connect()
        await bus.memory_write("content", metadata={"custom_key": "custom_val"})

        event = _parse_event(js.published[0][1])
        assert event.payload["custom_key"] == "custom_val"


class TestDecision:
    @pytest.mark.asyncio
    async def test_publishes_audit_accepted(self):
        bus, js = _make_bus()
        await bus.connect()
        ok = await bus.decision(
            "chose lane A because latency is lower",
            chosen="lane-A",
            alternatives=["lane-B", "lane-C"],
        )

        assert ok is True
        event = _parse_event(js.published[0][1])
        assert event.event_type == EventType.AUDIT_ACCEPTED
        assert event.payload["chosen"] == "lane-A"
        assert event.payload["alternatives"] == ["lane-B", "lane-C"]
        assert "chose lane A" in event.payload["reasoning"]

    @pytest.mark.asyncio
    async def test_truncates_long_reasoning(self):
        bus, js = _make_bus()
        await bus.connect()
        long_reason = "r" * 2000
        await bus.decision(long_reason)

        event = _parse_event(js.published[0][1])
        assert len(event.payload["reasoning"]) <= 1024


class TestEmitDisabled:
    @pytest.mark.asyncio
    async def test_emit_false_suppresses_publish(self):
        bus, js = _make_bus(emit=False)
        await bus.connect()
        ok = await bus.task_completed()
        assert ok is True
        assert len(js.published) == 0

    @pytest.mark.asyncio
    async def test_all_hooks_suppressed_when_disabled(self):
        bus, js = _make_bus(emit=False)
        await bus.connect()
        await bus.memory_write("something")
        await bus.decision("a reason")
        assert len(js.published) == 0


class TestMetrics:
    @pytest.mark.asyncio
    async def test_published_count_increments(self):
        bus, js = _make_bus()
        await bus.connect()
        await bus.task_completed()
        await bus.memory_write("x")
        await bus.decision("y")

        m = bus.metrics()
        assert m["published_count"] == 3
        assert m["failure_count"] == 0
        assert m["agent_id"] == "test-agent"

    @pytest.mark.asyncio
    async def test_failure_count_increments_on_nats_error(self):
        bus, js = _make_bus()
        await bus.connect()

        # Inject a failure into the fake jetstream
        async def _fail(subject, data):
            raise RuntimeError("nats down")

        js.publish = _fail  # type: ignore

        ok = await bus.task_completed()
        assert ok is False
        assert bus.failure_count == 1
        assert bus._last_error is not None


class TestContextManager:
    @pytest.mark.asyncio
    async def test_context_manager_connects_and_closes(self):
        mgr = _FakeManager()
        bus = SessionEventBus(agent_id="ctx-test", cluster_manager=mgr)

        async with bus:
            assert bus._connected is True
            await bus.task_completed()

        assert bus._connected is False


class TestUUIDCoercion:
    def test_none_returns_new_uuid(self):
        result = SessionEventBus._coerce_uuid(None)
        assert isinstance(result, UUID)

    def test_uuid_passthrough(self):
        uid = uuid4()
        assert SessionEventBus._coerce_uuid(uid) == uid

    def test_string_parsed(self):
        uid = uuid4()
        assert SessionEventBus._coerce_uuid(str(uid)) == uid


class TestModuleLevelHooks:
    @pytest.mark.asyncio
    async def test_hook_task_completed(self, monkeypatch):
        published: list[tuple] = []

        async def _fake_fire(agent_id, event_type, payload, task_id=None):
            published.append((agent_id, event_type, payload))
            return True

        import memu.session_bus as sb
        monkeypatch.setattr(sb, "_fire_and_forget", _fake_fire)

        ok = await hook_session_task_completed("mack", result={"ok": True}, tokens_used=42)
        assert ok is True
        assert published[0][0] == "mack"
        assert published[0][1] == EventType.TASK_COMPLETED

    @pytest.mark.asyncio
    async def test_hook_memory_write(self, monkeypatch):
        published: list[tuple] = []

        async def _fake_fire(agent_id, event_type, payload, task_id=None):
            published.append((agent_id, event_type, payload))
            return True

        import memu.session_bus as sb
        monkeypatch.setattr(sb, "_fire_and_forget", _fake_fire)

        ok = await hook_session_memory_write("mack", "stored plan", namespace="plan")
        assert ok is True
        assert published[0][1] == EventType.CHECKPOINT_SAVED

    @pytest.mark.asyncio
    async def test_hook_decision(self, monkeypatch):
        published: list[tuple] = []

        async def _fake_fire(agent_id, event_type, payload, task_id=None):
            published.append((agent_id, event_type, payload))
            return True

        import memu.session_bus as sb
        monkeypatch.setattr(sb, "_fire_and_forget", _fake_fire)

        ok = await hook_session_decision("mack", "chose X", chosen="X")
        assert ok is True
        assert published[0][1] == EventType.AUDIT_ACCEPTED


class TestSwarmEventValidity:
    """Every published event must be a valid SwarmEvent — no schema drift."""

    @pytest.mark.asyncio
    async def test_all_three_hooks_produce_valid_swarm_events(self):
        bus, js = _make_bus(agent_id="validity-check")
        await bus.connect()

        await bus.task_completed(result={"done": True})
        await bus.memory_write("wrote to memU")
        await bus.decision("chose path", chosen="A")

        assert len(js.published) == 3
        for _, raw in js.published:
            event = _parse_event(raw)
            assert event.source_gateway == "validity-check"
            assert isinstance(event.task_id, UUID)
            assert isinstance(event.event_id, UUID)
            assert isinstance(event.timestamp, __import__("datetime").datetime)
