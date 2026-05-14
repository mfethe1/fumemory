from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

import memu.boot as boot


class _FakeNats:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))


class _FakeCluster:
    def __init__(self) -> None:
        self.active_connection = _FakeNats()


@pytest.mark.asyncio
async def test_start_heartbeat_uses_module_safe_tenant_subject(monkeypatch):
    monkeypatch.setenv("TENANT_ID", "acme")
    monkeypatch.setattr(boot, "TASK_ID", "task-123")

    async def stop_after_publish(_interval_s: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(boot.asyncio, "sleep", stop_after_publish)
    cluster = _FakeCluster()

    await boot.start_heartbeat(cluster, interval_s=0)

    assert cluster.active_connection.published
    assert cluster.active_connection.published[0][0] == "tenant.acme.swarm.warden.heartbeat"


@pytest.mark.asyncio
async def test_gateway_offline_publish_uses_module_safe_tenant_subject(monkeypatch):
    monkeypatch.setenv("TENANT_ID", "acme")
    cluster = _FakeCluster()

    await boot._publish_gateway_offline(cluster)

    assert cluster.active_connection.published
    assert cluster.active_connection.published[0][0] == "tenant.acme.swarm.discovery"


def _compose_services() -> dict:
    return yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))["services"]


def test_compose_api_uses_canonical_ollama_embedding_env():
    api_env = _compose_services()["api"]["environment"]

    assert api_env["EMBEDDING_API_BASE"] == "http://ollama:11434"
    assert "EMBEDDING_BASE_URL" not in api_env


def test_compose_bridge_receives_local_nats_defaults():
    bridge_env = _compose_services()["bridge"]["environment"]

    assert bridge_env["NATS_LOCAL_URL"] == "nats://nats:4222"
    assert bridge_env["NATS_RAILWAY_URL"] == "${NATS_RAILWAY_URL:-nats://nats:4222}"
