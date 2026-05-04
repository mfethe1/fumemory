from __future__ import annotations

import pytest

from memu import temporal_client


@pytest.mark.asyncio
async def test_store_memory_workflow_degrades_without_temporal(monkeypatch):
    monkeypatch.setattr(temporal_client, "get_client", _raise_missing_temporal)

    req_dict = {
        "content": "hello world",
        "agent_id": "lenny",
        "memory_type": "observation",
        "memory_kind": "learning",
        "idempotency_key": None,
        "salience_score": 0.5,
        "metadata": {"source": "test"},
        "allowed_roles": ["*"],
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "parent_id": None,
    }
    workflow_id = await temporal_client.store_memory_workflow(req_dict)

    assert workflow_id is None


@pytest.mark.asyncio
async def test_search_memory_workflow_degrades_without_temporal(monkeypatch):
    monkeypatch.setattr(temporal_client, "get_client", _raise_missing_temporal)

    result = await temporal_client.search_memory_workflow("hello", "lenny")

    assert result is None


def test_workflow_suffix_is_stable():
    first = temporal_client._workflow_suffix("lenny", "same-content")
    second = temporal_client._workflow_suffix("lenny", "same-content")
    other = temporal_client._workflow_suffix("lenny", "different-content")

    assert first == second
    assert first != other
    assert len(first) == 16


async def _raise_missing_temporal():
    raise RuntimeError("temporalio is not installed")
