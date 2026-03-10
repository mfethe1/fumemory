from __future__ import annotations

import pytest

from memu import temporal_client


@pytest.mark.asyncio
async def test_store_memory_workflow_degrades_without_temporal(monkeypatch):
    monkeypatch.setattr(temporal_client, "get_client", _raise_missing_temporal)

    workflow_id = await temporal_client.store_memory_workflow(
        "hello world",
        "lenny",
        {"source": "test"},
    )

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
