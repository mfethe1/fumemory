from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from memu.state_manager import (
    GATEWAY_STATE_BUCKET,
    GLOBAL_SETTINGS_BUCKET,
    StateManager,
    build_gateway_status_overview,
)


class FakeEntry:
    def __init__(self, value: bytes):
        self.value = value


class FakeKV:
    def __init__(self, initial: dict[str, dict] | None = None):
        self.data = {
            key: json.dumps(value).encode()
            for key, value in (initial or {}).items()
        }
        self.revision = 0

    async def put(self, key: str, value: bytes, validate_keys: bool = True):
        self.revision += 1
        self.data[key] = value
        return self.revision

    async def get(self, key: str, revision=None, validate_keys: bool = True):
        return FakeEntry(self.data[key])

    async def keys(self, filters=None, **kwargs):
        return list(self.data.keys())


class FakeJS:
    def __init__(self, buckets: dict[str, FakeKV] | None = None):
        self.buckets = buckets or {}

    async def key_value(self, bucket: str):
        if bucket not in self.buckets:
            raise KeyError(bucket)
        return self.buckets[bucket]

    async def create_key_value(self, config=None, **params):
        bucket = config.bucket if config else params["bucket"]
        kv = FakeKV()
        self.buckets[bucket] = kv
        return kv


class FakeNC:
    def __init__(self):
        self.is_connected = True
        self.messages: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: bytes):
        self.messages.append((subject, json.loads(payload.decode())))


@pytest.mark.asyncio
async def test_build_gateway_status_overview_flags_stale_and_cluster_fallback():
    now = datetime.now(timezone.utc)
    js = FakeJS(
        {
            GATEWAY_STATE_BUCKET: FakeKV(
                {
                    "mack": {
                        "gateway_id": "mack",
                        "status": "online",
                        "last_heartbeat_at": (now - timedelta(seconds=5)).isoformat(),
                        "current_task_id": "task-1",
                    },
                    "winnie": {
                        "gateway_id": "winnie",
                        "status": "online",
                        "last_heartbeat_at": (now - timedelta(seconds=25)).isoformat(),
                    },
                    "rosie": {
                        "gateway_id": "rosie",
                        "status": "draining",
                        "last_heartbeat_at": (now - timedelta(seconds=3)).isoformat(),
                    },
                }
            )
        }
    )

    overview = await build_gateway_status_overview(
        js,
        cluster_status={
            "active_node": "railway",
            "nodes": {
                "local": {"connected": False},
                "railway": {"connected": True},
            },
        },
        stale_after_s=20,
        offline_after_s=60,
    )

    assert overview["summary"]["online"] == 1
    assert overview["summary"]["degraded"] == 1
    assert overview["summary"]["draining"] == 1
    assert overview["summary"]["active_tasks"] == 1
    assert overview["summary"]["stale_gateways"] == ["winnie"]

    alert_codes = {alert["code"] for alert in overview["alerts"]}
    assert "cluster_on_fallback" in alert_codes
    assert "cluster_nodes_disconnected" in alert_codes
    assert "gateway_stale" in alert_codes


@pytest.mark.asyncio
async def test_state_manager_registers_and_publishes_gateway_status_contract():
    state_kv = FakeKV()
    settings_kv = FakeKV()
    js = FakeJS(
        {
            GATEWAY_STATE_BUCKET: state_kv,
            GLOBAL_SETTINGS_BUCKET: settings_kv,
        }
    )
    nc = FakeNC()

    manager = StateManager("mack", role="infra")
    manager.nc = nc
    manager.js = js
    manager.state_kv = state_kv
    manager.settings_kv = settings_kv

    await manager.register_gateway(
        current_task_id="task-123",
        metadata={"environment": "desktop", "host": "mac-mini"},
    )
    await manager.mark_offline(
        current_task_id="task-123",
        metadata={"reason": "test-shutdown"},
    )

    subjects = [subject for subject, _ in nc.messages]
    assert "swarm.gateway.register" in subjects
    assert "swarm.discovery" in subjects
    assert "swarm.gateway.heartbeat" in subjects
    assert "swarm.gateway.status" in subjects

    final_state = json.loads(state_kv.data["mack"].decode())
    assert final_state["status"] == "offline"
    assert final_state["current_task_id"] == "task-123"
    assert final_state["metadata"]["environment"] == "desktop"
    assert final_state["metadata"]["reason"] == "test-shutdown"
    assert final_state["active_nats_node"] == "standalone"
