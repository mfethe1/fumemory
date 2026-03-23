"""Live NATS integration coverage for AGENT_EVENTS stream/consumer recovery."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

nats = pytest.importorskip("nats")

from memu.agent_events_consumer import AgentEventsConsumer

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")


class _LiveManager:
    def __init__(self, nc, js):
        self._nc = nc
        self._js = js
        self.active_node = SimpleNamespace(value="local")
        self.active_connection = nc

    async def connect(self):
        return None

    async def close(self):
        if self._nc.is_connected:
            await self._nc.close()

    def status(self):
        return {
            "active_node": "local",
            "nodes": {"local": {"connected": self._nc.is_connected, "url": NATS_URL}},
        }

    @property
    def jetstream(self):
        return self._js


async def _wait_for(predicate, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met before timeout")


@pytest.mark.asyncio
async def test_agent_events_consumer_recovers_subject_drift_on_live_nats():
    try:
        nc = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=1.0), timeout=2.0)
    except Exception:
        pytest.skip(f"NATS unavailable at {NATS_URL}")

    js = nc.jetstream()
    suffix = uuid4().hex[:8]
    stream = f"AGENT_EVENTS_IT_{suffix}"
    subject = f"AGENT_EVENTS.it.{suffix}"
    consumer_name = f"MEMU_AGENT_EVENTS_IT_{suffix}"

    consumer = None
    try:
        await js.add_stream(name=stream, subjects=[f"drift.old.{suffix}"])
        manager = _LiveManager(nc, js)
        consumer = AgentEventsConsumer(
            stream=stream,
            consumer_name=consumer_name,
            subject=subject,
            cluster_manager=manager,
        )

        await consumer.start()
        info = await js.stream_info(stream)
        assert subject in info.config.subjects

        await js.publish(subject, json.dumps({"event_type": "task_completed", "ok": True}).encode())
        await _wait_for(lambda: consumer.metrics.acked_count == 1)

        assert consumer.metrics.failure_count == 0
        assert consumer.metrics.consumed_count == 1
    finally:
        if consumer is not None:
            try:
                await consumer.stop()
            except Exception:
                pass
        try:
            await js.delete_stream(stream)
        except Exception:
            pass
        if nc.is_connected:
            await nc.close()
