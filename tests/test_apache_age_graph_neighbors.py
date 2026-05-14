"""
Tests for the Apache AGE graph-neighbors endpoint.
All tests use mocks — no live DB required.
"""
import pathlib
import sys
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memu import api
from memu.api import get_graph_neighbors


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


VALID_UUID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.asyncio
async def test_get_graph_neighbors_returns_rows(monkeypatch):
    """Endpoint returns neighbors list on success."""
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"rel_type": "DERIVED_FROM", "neighbor": '{"id": "00000000-0000-0000-0000-000000000002"}'}
    ]
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    result = await get_graph_neighbors(memory_id=VALID_UUID, _key="memu-dev-key")

    assert result["ok"] is True
    assert len(result["neighbors"]) == 1
    conn.execute.assert_awaited_once_with('SET search_path = ag_catalog, "$user", public')


@pytest.mark.asyncio
async def test_get_graph_neighbors_empty(monkeypatch):
    """Endpoint returns empty list when no neighbors exist."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    result = await get_graph_neighbors(memory_id=VALID_UUID, _key="memu-dev-key")

    assert result["ok"] is True
    assert result["neighbors"] == []


@pytest.mark.asyncio
async def test_get_graph_neighbors_uuid_interpolated_in_cypher(monkeypatch):
    """UUID is embedded in the Cypher query string sent to the DB."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    await get_graph_neighbors(memory_id=VALID_UUID, _key="memu-dev-key")

    fetch_sql = conn.fetch.await_args.args[0]
    assert str(VALID_UUID) in fetch_sql
    assert "memu_graph" in fetch_sql


@pytest.mark.asyncio
async def test_get_graph_neighbors_custom_graph_name(monkeypatch):
    """Custom graph_name is used in the Cypher query instead of the default."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    await get_graph_neighbors(memory_id=VALID_UUID, graph_name="test_graph", _key="memu-dev-key")

    fetch_sql = conn.fetch.await_args.args[0]
    assert "test_graph" in fetch_sql
    assert "memu_graph" not in fetch_sql


@pytest.mark.asyncio
async def test_get_graph_neighbors_invalid_graph_name_raises_422(monkeypatch):
    """Invalid graph_name (SQL injection attempt) raises HTTP 422."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_graph_neighbors(memory_id=VALID_UUID, graph_name="'; DROP TABLE memories; --", _key="memu-dev-key")

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_get_graph_neighbors_db_error_raises_400(monkeypatch):
    """DB errors are surfaced as HTTP 400."""
    conn = AsyncMock()
    conn.fetch.side_effect = Exception("connection refused")
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_graph_neighbors(memory_id=VALID_UUID, _key="memu-dev-key")

    assert exc.value.status_code == 400
    assert "connection refused" in str(exc.value.detail)
