"""Runtime instrumentation smoke tests for STATE_EVENTS/STATE_PROJECTOR evidence."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from memu.lane_lock import _STATE_EVENT_EMITS, get_state_event_evidence, LaneLockedExecution
from memu.state_projector import StateProjectorConsumer
from memu.swarm_models import EventType, SwarmEvent


class _DummyJS:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:  # pragma: no cover
        self.published.append((subject, payload))


def test_lane_lock_state_event_instrumentation_records_emitted_events():
    """LaneLockedExecution should log emitted state-event evidence for projector tracing."""

    task_id = uuid4()
    event = SwarmEvent(
        source_gateway="edge-1",
        event_type=EventType.LANE_ACQUIRED,
        task_id=task_id,
        payload={"lane": "lane:test", "gateway_id": "agent-1"},
    )

    _STATE_EVENT_EMITS.clear()
    js = _DummyJS()
    executor = object.__new__(LaneLockedExecution)
    executor.js = js
    executor.gateway_id = "gateway-1"
    executor._state_event_version = 11

    asyncio.run(executor._publish_state_event(event))

    assert len(js.published) == 1
    assert _STATE_EVENT_EMITS
    evidence = get_state_event_evidence(limit=1)[-1]
    assert evidence["event_id"] == str(event.event_id)
    assert evidence["version"] == 11
    assert evidence["emitted"]


def test_state_projector_tracks_applied_event_sequence():
    """Projector keeps a bounded in-memory applied-event sequence for evidence."""

    payload = {
        "event_id": str(uuid4()),
        "gateway_id": "gateway-2",
        "agent_id": "agent-2",
        "event_type": "task_claimed",
        "entity_id": str(uuid4()),
        "ts": "2026-01-01T00:00:00Z",
        "version": 7,
        "payload": {"x": "y"},
    }

    consumer = StateProjectorConsumer()
    consumer._record_projection(payload)
    assert consumer.applied_event_trace()[-1]["event_id"] == payload["event_id"]
    assert consumer.applied_event_trace()[-1]["version"] == payload["version"]
    assert isinstance(consumer.applied_event_trace()[-1]["applied_at"], str)
