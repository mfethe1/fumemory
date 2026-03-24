"""
Tests for the Apache AGE graph-stats endpoint (/api/v1/memu/graph/stats).
All tests use mocks — no live DB required.
"""
import pathlib
import sys
from unittest.mock import AsyncMock, call

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memu import api
from memu.api import get_graph_stats


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


def _make_conn(vertex_count=5, edge_count=3, rel_types=None):
    """Return a mock asyncpg connection with preset fetch responses."""
    conn = AsyncMock()
    if rel_types is None:
        rel_types = [("DERIVED_FROM", 2), ("SIMILAR_TO", 1)]

    v_row = {"cnt": str(vertex_count)}
    e_row = {"cnt": str(edge_count)}
    rt_rows = [{"rel_type": f'"{rt}"', "cnt": str(cnt)} for rt, cnt in rel_types]

    conn.fetch.side_effect = [
        [v_row],   # vertex count query
        [e_row],   # edge count query
        rt_rows,   # rel-type breakdown query
    ]
    return conn


@pytest.mark.asyncio
async def test_graph_stats_returns_ok_and_counts(monkeypatch):
    """Returns ok=True with correct vertex/edge counts."""
    conn = _make_conn(vertex_count=10, edge_count=4)
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    result = await get_graph_stats(_key="memu-dev-key")

    assert result["ok"] is True
    assert result["vertex_count"] == 10
    assert result["edge_count"] == 4
    assert result["graph_name"] == "memu_graph"


@pytest.mark.asyncio
async def test_graph_stats_returns_top_rel_types(monkeypatch):
    """top_rel_types contains labelled counts sorted by the DB."""
    conn = _make_conn(rel_types=[("DERIVED_FROM", 7), ("SIMILAR_TO", 3)])
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    result = await get_graph_stats(_key="memu-dev-key")

    assert len(result["top_rel_types"]) == 2
    assert result["top_rel_types"][0] == {"rel_type": "DERIVED_FROM", "count": 7}
    assert result["top_rel_types"][1] == {"rel_type": "SIMILAR_TO", "count": 3}


@pytest.mark.asyncio
async def test_graph_stats_empty_graph(monkeypatch):
    """Empty graph returns zero counts and empty rel_types list."""
    conn = AsyncMock()
    conn.fetch.side_effect = [
        [{"cnt": "0"}],
        [{"cnt": "0"}],
        [],
    ]
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    result = await get_graph_stats(_key="memu-dev-key")

    assert result["ok"] is True
    assert result["vertex_count"] == 0
    assert result["edge_count"] == 0
    assert result["top_rel_types"] == []


@pytest.mark.asyncio
async def test_graph_stats_custom_graph_name_in_queries(monkeypatch):
    """Custom graph_name appears in all three cypher queries."""
    conn = _make_conn()
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    await get_graph_stats(graph_name="my_graph", _key="memu-dev-key")

    for fetch_call in conn.fetch.call_args_list:
        assert "my_graph" in fetch_call.args[0]
        assert "memu_graph" not in fetch_call.args[0]


@pytest.mark.asyncio
async def test_graph_stats_invalid_graph_name_raises_422(monkeypatch):
    """SQL-injection attempt in graph_name raises HTTP 422."""
    conn = AsyncMock()
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_graph_stats(graph_name="'; DROP TABLE memories; --", _key="memu-dev-key")

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_graph_stats_search_path_set(monkeypatch):
    """search_path is set to ag_catalog before any query."""
    conn = _make_conn()
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    await get_graph_stats(_key="memu-dev-key")

    conn.execute.assert_awaited_once_with('SET search_path = ag_catalog, "$user", public')


@pytest.mark.asyncio
async def test_graph_stats_db_error_raises_400(monkeypatch):
    """DB errors surface as HTTP 400 with the original message."""
    conn = AsyncMock()
    conn.fetch.side_effect = Exception("connection reset")
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    with pytest.raises(HTTPException) as exc:
        await get_graph_stats(_key="memu-dev-key")

    assert exc.value.status_code == 400
    assert "connection reset" in str(exc.value.detail)
