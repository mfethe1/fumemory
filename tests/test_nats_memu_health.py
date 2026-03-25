import pytest
import asyncio
import os
from memu.agent_events_consumer import AgentEventsConsumer
from memu.cluster import NATSClusterManager

os.environ["NATS_RAILWAY_URL"] = "nats://test-mock-railway:4222"

@pytest.mark.asyncio
async def test_agent_events_health_artifact():
    # Test that the consumer can generate a health artifact
    # without blowing up, even if it hasn't fully connected.
    consumer = AgentEventsConsumer()
    
    artifact = await consumer.health_artifact()
    
    assert artifact["source"] == "AGENT_EVENTS"
    assert "stream" in artifact
    assert "consumer" in artifact
    assert "durable_active" in artifact
    assert "metrics" in artifact
    assert "lag" in artifact
    
    metrics = artifact["metrics"]
    assert "consumed_count" in metrics
    assert "acked_count" in metrics
    assert "failure_count" in metrics
    assert "replay_count" in metrics

@pytest.mark.asyncio
async def test_nats_cluster_manager_status():
    manager = NATSClusterManager()
    status = manager.status()
    
    assert "active_node" in status
    assert "nodes" in status
    
    assert "local" in status["nodes"]
    assert "railway" in status["nodes"]
    
    local_node = status["nodes"]["local"]
    assert "connected" in local_node
    assert "latency_ms" in local_node
