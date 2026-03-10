from __future__ import annotations

from uuid import uuid4

from memu.temporal_contracts import (
    TEMPORAL_ROUTE_VERSION,
    coerce_orchestration_context,
    canonical_workflow_id,
    load_temporal_settings,
    temporal_tls_enabled,
)


def test_canonical_workflow_id_is_stable_and_order_insensitive_for_dicts():
    payload_a = {"query": "checkpoint", "limit": 5}
    payload_b = {"limit": 5, "query": "checkpoint"}

    first = canonical_workflow_id("memory-search", "winnie", payload_a, "lease-1")
    second = canonical_workflow_id("memory-search", "winnie", payload_b, "lease-1")

    assert first == second
    assert first.startswith("memory-search-")
    assert len(first) > len("memory-search-")


def test_coerce_orchestration_context_supports_metadata_backcompat():
    task_id = uuid4()
    ctx = coerce_orchestration_context(
        metadata={
            "task_id": str(task_id),
            "source_gateway": "mack",
            "orchestration": {
                "lease_key": "telegram:chat:topic",
                "checkpoint_scope": "cross_gateway",
            },
        }
    )

    assert ctx is not None
    assert ctx.task_id == task_id
    assert ctx.source_gateway == "mack"
    assert ctx.lease_key == "telegram:chat:topic"
    assert ctx.checkpoint_scope == "cross_gateway"


def test_load_temporal_settings_reads_namespace_queue_and_identity():
    settings = load_temporal_settings(
        {
            "TEMPORAL_HOST": "temporal.example:7233",
            "TEMPORAL_NAMESPACE": "swarm-prod",
            "TEMPORAL_TASK_QUEUE": "gateway-handoff",
            "TEMPORAL_WORKER_IDENTITY": "worker:mack:1234",
        }
    )

    assert settings.host == "temporal.example:7233"
    assert settings.namespace == "swarm-prod"
    assert settings.task_queue == "gateway-handoff"
    assert settings.worker_identity == "worker:mack:1234"


def test_temporal_tls_enabled_detects_explicit_tls_or_port_443():
    assert temporal_tls_enabled("temporal.example:443", {}) is True
    assert temporal_tls_enabled("temporal.example:7233", {"TEMPORAL_TLS": "true"}) is True
    assert temporal_tls_enabled("temporal.example:7233", {}) is False


def test_route_version_constant_is_pinned_for_contracts():
    assert TEMPORAL_ROUTE_VERSION == "2026-03-10"
