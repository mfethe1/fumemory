from memu.cluster import NATSClusterManager


def test_clean_url_preserves_service_dns_hosts():
    assert NATSClusterManager._clean_url("nats://nats:4222") == "nats://nats:4222"
    assert (
        NATSClusterManager._clean_url("nats://nats.railway.internal:4222")
        == "nats://nats.railway.internal:4222"
    )
    assert (
        NATSClusterManager._clean_url("nats://memu-nats.railway.internal:4222")
        == "nats://memu-nats.railway.internal:4222"
    )


def test_clean_url_filters_only_empty_sentinels():
    for raw in [None, "", "   ", "none", "null", "undefined", "-", "-1"]:
        assert NATSClusterManager._clean_url(raw) is None
