"""Tests for AGENT_EVENTS durable consumer metrics + health/report exposure."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from memu.agent_events_consumer import AgentEventsConsumer, AgentEventsMetrics
from memu import api


class _FakeNatsConnection:
    def __init__(self, connected: bool = True):
        self.is_connected = connected


class _FakeStreamConfig:
    def __init__(self, subjects: list[str] | None = None):
        self.name = "AGENT_EVENTS"
        self.description = "existing"
        self.subjects = subjects or []
        self.retention = "limits"
        self.max_consumers = -1
        self.max_msgs = 1000
        self.max_bytes = -1
        self.discard = "old"
        self.max_age = 60
        self.max_msgs_per_subject = -1
        self.max_msg_size = -1
        self.storage = "file"
        self.num_replicas = 1
        self.duplicate_window = 120
        self.sealed = False
        self.deny_delete = False
        self.deny_purge = False
        self.allow_rollup_hdrs = False
        self.allow_direct = True
        self.mirror_direct = False
        self.compression = "none"
        self.allow_msg_ttl = False
        self.metadata = {"_nats.req.level": "0"}


class _FakeJS:
    def __init__(self, subjects: list[str] | None = None, stream_missing: bool = True):
        self.stream_missing = stream_missing
        self.config = _FakeStreamConfig(subjects)
        self.add_stream_calls: list[dict[str, Any]] = []
        self.update_stream_calls: list[Any] = []
        self.add_consumer_calls: list[dict[str, Any]] = []

    async def stream_info(self, _stream_name):
        if self.stream_missing:
            raise RuntimeError("missing")
        return SimpleNamespace(config=self.config)

    async def add_stream(self, **kwargs):
        self.add_stream_calls.append(kwargs)
        self.config = _FakeStreamConfig(kwargs.get("subjects", []))
        self.stream_missing = False

    async def update_stream(self, config=None, **kwargs):
        self.update_stream_calls.append(config or kwargs)
        if config is not None:
            self.config = config
        elif "subjects" in kwargs:
            self.config.subjects = kwargs["subjects"]
        self.stream_missing = False

    async def consumer_info(self, _stream_name, _consumer_name):
        raise RuntimeError("missing")

    async def add_consumer(self, **kwargs):
        self.add_consumer_calls.append(kwargs)


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


class _FakeMsg:
    def __init__(self, payload: str, num_delivered: int = 1, fail_ack: bool = False, has_metadata: bool = True):
        self.data = payload.encode()
        self.failed_ack = fail_ack
        self.acked = False
        self.naked = False
        self.metadata = SimpleNamespace(num_delivered=num_delivered) if has_metadata else None

    def ack(self):
        if self.failed_ack:
            raise RuntimeError("ack failed")
        self.acked = True

    def ack_sync(self):
        self.ack()

    def nak(self):
        self.naked = True

    def term(self):
        self.naked = True


class _FakeConnection:
    async def fetchval(self, _query):
        return 1


class _FakeAcquire:
    def __init__(self):
        self.conn = _FakeConnection()

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakePool:
    def acquire(self):
        return _FakeAcquire()


@pytest.mark.asyncio
async def test_agent_events_consumer_creates_stream_with_subject_and_filtered_consumer():
    fake_js = _FakeJS(stream_missing=True)
    consumer = AgentEventsConsumer(cluster_manager=_FakeManager(fake_js))
    consumer._js = fake_js

    await consumer._ensure_stream()
    await consumer._ensure_consumer()

    assert fake_js.add_stream_calls[0]["subjects"] == [consumer.subject]
    assert fake_js.add_consumer_calls == []


@pytest.mark.asyncio
async def test_agent_events_consumer_reconciles_existing_stream_subject_drift():
    fake_js = _FakeJS(subjects=["events.>", "agent.>", "swarm.events"], stream_missing=False)
    consumer = AgentEventsConsumer(cluster_manager=_FakeManager(fake_js))
    consumer._js = fake_js

    await consumer._ensure_stream()

    assert len(fake_js.update_stream_calls) == 1
    assert consumer.subject in fake_js.config.subjects


@pytest.mark.asyncio
async def test_agent_events_consumer_process_message_tracks_metrics_and_replays():
    consumer = AgentEventsConsumer(cluster_manager=_FakeManager())

    await consumer.process_message(_FakeMsg('{"event_type":"task_completed"}', num_delivered=1))
    await consumer.process_message(_FakeMsg('{"event_type":"decision"}', num_delivered=3))

    assert consumer.metrics.consumed_count == 2
    assert consumer.metrics.acked_count == 2
    assert consumer.metrics.failure_count == 0
    assert consumer.metrics.replay_count == 2
    assert consumer.metrics.ack_rate == 1.0
    assert consumer.metrics.failure_rate == 0.0


@pytest.mark.asyncio
async def test_agent_events_consumer_failure_increments_failure_rate():
    consumer = AgentEventsConsumer(cluster_manager=_FakeManager())

    await consumer.process_message(_FakeMsg('{not-json', num_delivered=1))
    await consumer.process_message(_FakeMsg('{"event_type":"ok"}', fail_ack=True))

    assert consumer.metrics.consumed_count == 2
    assert consumer.metrics.failure_count == 2
    assert consumer.metrics.acked_count == 0
    assert consumer.metrics.ack_rate == 0.0
    assert consumer.metrics.failure_rate == 1.0


@pytest.mark.asyncio
async def test_health_report_endpoint_exposes_agent_events_artifact():
    original_pool = api.pool
    original_consumer = api.agent_events_consumer

    api.pool = _FakePool()
    async def _artifact():
        return {"enabled": True, "durable_active": True}

    api.agent_events_consumer = SimpleNamespace(
        health_artifact=_artifact,
    )

    try:
        payload = await api._agent_events_report()
        assert payload["enabled"] is True
        assert payload["durable_active"] is True

        report = await api.health()
        assert report["status"] == "healthy"
        assert report["agent_events"]["enabled"] is True

        report_payload = await api.health_report()
        assert report_payload["artifact"] == "agent_events"
        assert report_payload["status"] == "healthy"
    finally:
        api.pool = original_pool
        api.agent_events_consumer = original_consumer


def test_agent_events_metrics_report_shape():
    metrics = AgentEventsMetrics(consumed_count=4, acked_count=2, replay_count=1, failure_count=2)
    data = metrics.as_dict()

    assert data["consumed_count"] == 4
    assert data["acked_count"] == 2
    assert data["replay_count"] == 1
    assert data["failure_rate"] == 0.5
    assert data["ack_rate"] == 0.5
