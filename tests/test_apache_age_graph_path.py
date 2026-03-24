"""
Tests for the Apache AGE graph-path endpoint (/api/v1/memu/graph/path/{from_id}/{to_id}).
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
from memu.api import get_graph_path


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


FROM_ID = UUID("00000000-0000-0000-0000-000000000001")
TO_ID   = UUID("00000000-0000-0000-0000-000000000002")


@pytest.mark.asyncio
async def test_get_graph_path_found(monkeypatch):
    """Returns path data when a path exists between two memories."""
    conn = AsyncMock()
    conn.fetch.return_value = [
        {
            "hops": 2,
            "node_ids": '["id-a","id-b","id-c"]',
            "rel_types": '["DERIVED_FROM","SIMILAR_TO"]',
        }
    ]
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    result = await get_graph_path(from_id=FROM_ID, to_id=TO_ID, _key="memu-dev-key")

    assert result["ok"] is True
    assert result["found"] is True
    assert result["path"]["hops"] == 2
    conn.execute.assert_awaited_once_with('SET search_path = ag_catalog, "$user", public')


@pytest.mark.asyncio
async def test_get_graph_path_not_found(monkeypatch):
    """Returns found=False and path=None when no path exists."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    result = await get_graph_path(from_id=FROM_ID, to_id=TO_ID, _key="memu-dev-key")

    assert result["ok"] is True
    assert result["found"] is False
    assert result["path"] is None


@pytest.mark.asyncio
async def test_get_graph_path_uuids_in_cypher(monkeypatch):
    """Both from_id and to_id UUIDs appear in the Cypher query."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    await get_graph_path(from_id=FROM_ID, to_id=TO_ID, _key="memu-dev-key")

    fetch_sql = conn.fetch.await_args.args[0]
    assert str(FROM_ID) in fetch_sql
    assert str(TO_ID) in fetch_sql


@pytest.mark.asyncio
async def test_get_graph_path_custom_graph_name(monkeypatch):
    """Custom graph_name is reflected in the Cypher query."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    await get_graph_path(from_id=FROM_ID, to_id=TO_ID, graph_name="test_graph", _key="memu-dev-key")

    fetch_sql = conn.fetch.await_args.args[0]
    assert "test_graph" in fetch_sql
    assert "memu_graph" not in fetch_sql


@pytest.mark.asyncio
async def test_get_graph_path_invalid_graph_name_raises_422(monkeypatch):
    """SQL-injection attempt in graph_name raises HTTP 422."""
    conn = AsyncMock()
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_graph_path(
            from_id=FROM_ID, to_id=TO_ID,
            graph_name="'; DROP TABLE memories; --",
            _key="memu-dev-key",
        )

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_get_graph_path_max_hops_too_low_raises_422(monkeypatch):
    """max_hops < 1 raises HTTP 422."""
    conn = AsyncMock()
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_graph_path(from_id=FROM_ID, to_id=TO_ID, max_hops=0, _key="memu-dev-key")

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_get_graph_path_max_hops_too_high_raises_422(monkeypatch):
    """max_hops > 10 raises HTTP 422."""
    conn = AsyncMock()
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_graph_path(from_id=FROM_ID, to_id=TO_ID, max_hops=11, _key="memu-dev-key")

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_get_graph_path_max_hops_in_cypher(monkeypatch):
    """max_hops value appears in the generated Cypher query."""
    conn = AsyncMock()
    conn.fetch.return_value = []
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    await get_graph_path(from_id=FROM_ID, to_id=TO_ID, max_hops=3, _key="memu-dev-key")

    fetch_sql = conn.fetch.await_args.args[0]
    assert "3" in fetch_sql


@pytest.mark.asyncio
async def test_get_graph_path_db_error_raises_400(monkeypatch):
    """DB errors surface as HTTP 400."""
    conn = AsyncMock()
    conn.fetch.side_effect = Exception("timeout")
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_graph_path(from_id=FROM_ID, to_id=TO_ID, _key="memu-dev-key")

    assert exc.value.status_code == 400
    assert "timeout" in str(exc.value.detail)
