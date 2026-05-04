from __future__ import annotations

import asyncio

import pytest

import memu.api as api
import memu.cluster as cluster_mod
from memu.cluster import ClusterNode, NATSClusterManager


def test_clean_url_strips_nullish_and_placeholder_values():
    assert NATSClusterManager._clean_url(None) is None
    assert NATSClusterManager._clean_url(" ") is None
    assert NATSClusterManager._clean_url("null") is None
    assert NATSClusterManager._clean_url("nats://nats.railway.internal:4222") is None
    assert NATSClusterManager._clean_url("nats://nats-railway.railway.internal:4222") is None


def test_clean_url_keeps_configured_railway_target_and_init_prefers_configured_node():
    manager = NATSClusterManager(
        local_url="none",
        railway_url="nats://railway-nats.example.com:4222",
    )

    assert manager.local_url is None
    assert manager.railway_url == "nats://railway-nats.example.com:4222"
    assert manager.health[ClusterNode.RAILWAY].url == "nats://railway-nats.example.com:4222"


def test_cluster_manager_default_keeps_gateway_reconnect_behavior():
    manager = NATSClusterManager(local_url="nats://127.0.0.1:4222", railway_url=None)

    assert manager.connect_timeout_s == 5.0
    assert manager.max_reconnect_attempts == -1
    assert manager.allow_reconnect is True


def test_api_startup_cluster_uses_one_shot_nats_connection(monkeypatch):
    monkeypatch.delenv("MEMU_NATS_STARTUP_CONNECT_TIMEOUT_S", raising=False)
    monkeypatch.delenv("MEMU_NATS_STARTUP_RECONNECT_ATTEMPTS", raising=False)

    manager = api._make_startup_nats_cluster()

    assert manager.connect_timeout_s == 2.0
    assert manager.max_reconnect_attempts == 0
    assert manager.allow_reconnect is False


def test_connect_node_passes_nonblocking_options_to_nats(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_connect(**kwargs):
        seen.update(kwargs)
        raise OSError("connection refused")

    monkeypatch.setattr(cluster_mod.nats, "connect", fake_connect)
    manager = NATSClusterManager(
        local_url=None,
        railway_url="nats://127.0.0.1:4222",
        connect_timeout_s=1.5,
        max_reconnect_attempts=0,
        allow_reconnect=False,
    )

    with pytest.raises(OSError):
        asyncio.run(manager._connect_node(ClusterNode.RAILWAY, manager.railway_url))

    assert seen["connect_timeout"] == 1.5
    assert seen["max_reconnect_attempts"] == 0
    assert seen["allow_reconnect"] is False
