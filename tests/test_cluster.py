from __future__ import annotations

import pytest

from memu.cluster import (
    DEFAULT_RAILWAY_INTERNAL_URL,
    ClusterNode,
    NATSClusterManager,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.is_connected = True

    def jetstream(self):
        return object()

    async def flush(self, timeout: int = 2):
        return None

    async def drain(self):
        return None

    async def close(self):
        return None


def test_requires_explicit_railway_url_outside_railway(monkeypatch):
    monkeypatch.delenv("NATS_RAILWAY_URL", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    monkeypatch.delenv("RAILWAY_SERVICE_ID", raising=False)
    monkeypatch.delenv("RAILWAY_REPLICA_ID", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)

    with pytest.raises(ValueError, match="NATS_RAILWAY_URL must be set outside Railway"):
        NATSClusterManager(local_url="nats://localhost:4222")


def test_uses_internal_url_inside_railway(monkeypatch):
    monkeypatch.delenv("NATS_RAILWAY_URL", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    manager = NATSClusterManager(local_url="nats://localhost:4222")

    assert manager.railway_url == DEFAULT_RAILWAY_INTERNAL_URL


@pytest.mark.asyncio
async def test_connect_uses_railway_token_only_for_railway_node(monkeypatch):
    monkeypatch.setenv("NATS_RAILWAY_URL", "nats://maglev.proxy.rlwy.net:55041")
    monkeypatch.setenv("NATS_RAILWAY_AUTH_TOKEN", "railway-secret")
    monkeypatch.delenv("NATS_LOCAL_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("NATS_AUTH_TOKEN", raising=False)

    captured: list[dict[str, object]] = []

    async def _fake_connect(**kwargs):
        captured.append(kwargs)
        return _FakeConnection()

    import memu.cluster as cluster_mod

    monkeypatch.setattr(cluster_mod.nats, "connect", _fake_connect)

    manager = NATSClusterManager(
        local_url="nats://localhost:4222",
        railway_url="nats://maglev.proxy.rlwy.net:55041",
        ping_interval_s=60,
    )
    await manager.connect()
    await manager.close()

    assert len(captured) == 2
    local_call = next(call for call in captured if call["servers"] == ["nats://localhost:4222"])
    railway_call = next(
        call
        for call in captured
        if call["servers"] == ["nats://maglev.proxy.rlwy.net:55041"]
    )
    assert "token" not in local_call
    assert railway_call["token"] == "railway-secret"
    assert manager.health[ClusterNode.RAILWAY].connected is True


@pytest.mark.asyncio
async def test_auth_token_argument_overrides_env(monkeypatch):
    monkeypatch.setenv("NATS_RAILWAY_URL", "nats://maglev.proxy.rlwy.net:55041")
    monkeypatch.setenv("NATS_RAILWAY_AUTH_TOKEN", "env-secret")

    captured: list[dict[str, object]] = []

    async def _fake_connect(**kwargs):
        captured.append(kwargs)
        return _FakeConnection()

    import memu.cluster as cluster_mod

    monkeypatch.setattr(cluster_mod.nats, "connect", _fake_connect)

    manager = NATSClusterManager(
        local_url="nats://localhost:4222",
        railway_url="nats://maglev.proxy.rlwy.net:55041",
        auth_token="explicit-secret",
        ping_interval_s=60,
    )
    await manager.connect()
    await manager.close()

    railway_call = next(
        call
        for call in captured
        if call["servers"] == ["nats://maglev.proxy.rlwy.net:55041"]
    )
    assert railway_call["token"] == "explicit-secret"
