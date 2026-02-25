"""Tests for STATE_EVENTS durable projector + health artifact."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("asyncpg")

from memu import api
from memu.state_projector import StateProjectorConsumer


class _FakeNatsConnection:
    def __init__(self, connected: bool = True):
        self.is_connected = connected


class _FakeConsumerInfo:
    pass


class _FakeJS:
    def __init__(self):
        self.streams = set()
        self.added_consumers: list[dict[str, Any]] = []
        self.stream_info_called = 0
        self.consumer_info_called = 0

    async def stream_info(self, stream_name):
        self.stream_info_called += 1
        raise RuntimeError("missing")

    async def add_stream(self, **kwargs):
        self.streams.add(kwargs["name"])

    async def consumer_info(self, stream_name, consumer_name):
        self.consumer_info_called += 1
        raise RuntimeError("missing")

    async def add_consumer(self, **kwargs):
        self.added_consumers.append(kwargs)

    async def subscribe(self, *args, **kwargs):
        raise RuntimeError("subscription not supported in this test")


class _FakeManager:
    def __init__(self, js: _FakeJS | None = None):
        self._status = {
            "active_node": "local",
            "nodes": {"local": {"connected": True, "url": "nats://localhost"}},
        }
        self.active_node = SimpleNamespace(value="local")
        self.active_connection = _FakeNatsConnection()
        self.js = js or _FakeJS()

    async def connect(self):
        return None

    async def close(self):
        return None

    def status(self):
        return self._status

    @property
    def jetstream(self):
        return self.js


class _FakeConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def fetchval(self, query: str):
        return 1


class _FakePool:
    def acquire(self):
        return _FakeConn()


class _RecordingConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args):
        self.calls.append((query, args))


class _FakeMsg:
    def __init__(self, payload: dict, num_delivered: int = 1, fail_ack: bool = False):
        import json

        self.data = json.dumps(payload).encode()
        self.metadata = SimpleNamespace(num_delivered=num_delivered)
        self.failed_ack = fail_ack
        self.acked = False
        self.naked = False

    def ack(self):
        if self.failed_ack:
            raise RuntimeError("ack failed")
        self.acked = True

    def ack_sync(self):
        self.acked = True

    def nak(self):
        self.naked = True

    def term(self):
        self.naked = True


class _ReplayTrackingSink:
    def __init__(self):
        self.events: list[dict] = []
        self.event_ids: set[str] = set()

    async def __call__(self, payload: dict):
        # Idempotent upsert behavior.
        if payload["event_id"] in self.event_ids:
            return
        self.event_ids.add(payload["event_id"])
        self.events.append(payload)


@pytest.mark.asyncio
async def test_state_projector_creates_durable_consumer_with_replay_policy():
    fake_js = _FakeJS()
    fake_manager = _FakeManager(fake_js)
    consumer = StateProjectorConsumer(cluster_manager=fake_manager)
    consumer._js = fake_manager.jetstream

    await consumer._ensure_stream()
    await consumer._ensure_consumer()

    assert fake_js.stream_info_called == 1
    assert fake_js.consumer_info_called == 1
    assert len(fake_js.added_consumers) == 1
    assert fake_js.added_consumers[0]["durable"] == "STATE_PROJECTOR"
    assert fake_js.added_consumers[0]["replay_policy"] == "original"
    assert fake_js.added_consumers[0]["ack_policy"] == "explicit"


@pytest.mark.asyncio
async def test_state_projector_persists_events_and_tracks_replays():
    sink = _ReplayTrackingSink()
    consumer = StateProjectorConsumer(cluster_manager=_FakeManager(), sink=sink)

    event = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "gateway_id": "gw-a",
        "agent_id": "agent-a",
        "event_type": "task.state.changed",
        "entity_id": "task-123",
        "ts": "2026-02-24T00:00:00Z",
        "version": 1,
        "payload": {"status": "claiming"},
    }

    # Initial delivery
    await consumer.process_message(_FakeMsg(event, num_delivered=1))
    assert consumer.metrics.consumed_count == 1
    assert consumer.metrics.acked_count == 1
    assert consumer.metrics.replay_count == 0
    assert consumer.metrics.failure_count == 0
    assert len(sink.events) == 1

    # Simulate same durable restart replay (redelivered)
    consumer_restart = StateProjectorConsumer(cluster_manager=_FakeManager(), sink=sink)
    await consumer_restart.process_message(_FakeMsg(event, num_delivered=2))

    # Replay was observed, but duplicate event is idempotently ignored by sink
    assert consumer_restart.metrics.consumed_count == 1
    assert consumer_restart.metrics.replay_count == 1
    assert consumer_restart.metrics.acked_count == 1
    assert len(sink.events) == 1


@pytest.mark.asyncio
async def test_state_projector_health_artifact_includes_lag_and_metrics():
    sink = _ReplayTrackingSink()
    consumer = StateProjectorConsumer(cluster_manager=_FakeManager(), sink=sink)
    consumer.metrics.consumed_count = 2
    consumer.metrics.acked_count = 1
    consumer.metrics.failure_count = 1
    consumer.metrics.last_error = "projection failed"

    artifact = await consumer.health_artifact()

    assert artifact["source"] == "STATE_EVENTS"
    assert artifact["consumer"] == "STATE_PROJECTOR"
    assert artifact["durable_active"] is False
    assert artifact["metrics"]["consumed_count"] == 2
    assert artifact["metrics"]["acked_count"] == 1
    assert artifact["metrics"]["failure_count"] == 1
    assert artifact["active_node"] == "local"
    assert artifact["active_node_connected"] is True


@pytest.mark.asyncio
async def test_health_report_endpoint_includes_state_projector_artifact():
    original_consumer = api.state_projector_consumer
    original_pool = api.pool

    async def _artifact():
        return {
            "enabled": True,
            "metrics": {"consumed_count": 3},
            "durable_active": True,
            "stream": "STATE_EVENTS",
        }

    api.pool = _FakePool()
    api.state_projector_consumer = SimpleNamespace(health_artifact=_artifact)

    try:
        report = await api.health_report()
        health = await api.health()
        assert report["artifact"] == "agent_events"
        assert report["state_projector"]["enabled"] is True
        assert report["state_projector"]["metrics"]["consumed_count"] == 3
        assert health["state_projector"]["enabled"] is True
    finally:
        api.state_projector_consumer = original_consumer
        api.pool = original_pool


@pytest.mark.asyncio
async def test_state_projector_validates_state_event_payload():
    consumer = StateProjectorConsumer(cluster_manager=_FakeManager(), sink=_ReplayTrackingSink())

    await consumer.process_message(_FakeMsg({"event_id": "x"}, num_delivered=1))

    assert consumer.metrics.consumed_count == 1
    assert consumer.metrics.failure_count == 1
    assert consumer.metrics.acked_count == 0


@pytest.mark.asyncio
async def test_state_projector_projects_backlog_events_into_backlog_tables():
    consumer = StateProjectorConsumer(cluster_manager=_FakeManager(), sink=_ReplayTrackingSink())
    conn = _RecordingConn()

    payload = {
        "event_id": "22222222-2222-4222-8222-222222222222",
        "gateway_id": "gw-a",
        "agent_id": "agent-main",
        "event_type": "backlog.task.synced",
        "entity_id": "oauth-auth-reliability",
        "ts": "2026-02-24T00:00:00Z",
        "version": 2,
        "payload": {
            "task_id": "oauth-auth-reliability",
            "owner": "Macklemore",
            "status": "in_progress",
            "next_action": "run live Google OAuth smoke matrix",
            "blocker": "needs OAuth creds",
            "source_file": "BACKLOG.md",
        },
    }

    await consumer._ensure_backlog_projection_tables(conn)
    await consumer._project_backlog_event(conn, payload)

    joined_queries = "\n".join(q for q, _ in conn.calls)
    assert "INSERT INTO backlog_items" in joined_queries
    assert "INSERT INTO backlog_events" in joined_queries
