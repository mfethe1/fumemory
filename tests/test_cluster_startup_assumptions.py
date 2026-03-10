from __future__ import annotations

from memu.cluster import ClusterNode, NATSClusterManager


def test_clean_url_strips_nullish_and_placeholder_values():
    assert NATSClusterManager._clean_url(None) is None
    assert NATSClusterManager._clean_url(" ") is None
    assert NATSClusterManager._clean_url("null") is None
    assert NATSClusterManager._clean_url("nats://nats.railway.internal:4222") is None
    assert NATSClusterManager._clean_url("nats://nats-railway.railway.internal:4222") is None


def test_clean_url_keeps_real_railway_target_and_init_prefers_configured_node():
    manager = NATSClusterManager(
        local_url="none",
        railway_url="nats://memu-production.up.railway.app:4222",
    )

    assert manager.local_url is None
    assert manager.railway_url == "nats://memu-production.up.railway.app:4222"
    assert manager.health[ClusterNode.RAILWAY].url == "nats://memu-production.up.railway.app:4222"
