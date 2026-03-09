import pathlib
import sys
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memu import api
from memu.api import CypherRequest, execute_cypher


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


@pytest.mark.asyncio
async def test_execute_cypher_uses_validated_graph_name(monkeypatch):
    conn = AsyncMock()
    conn.fetch.return_value = [{"result": "ok"}]
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    req = CypherRequest(query="MATCH (v:Memory) RETURN v", graph_name="memu_graph")
    result = await execute_cypher(req=req, _key="memu-dev-key")

    assert result["ok"] is True
    conn.execute.assert_awaited_once_with('SET search_path = ag_catalog, "$user", public')
    fetch_sql = conn.fetch.await_args.args[0]
    assert "cypher('memu_graph'" in fetch_sql


@pytest.mark.asyncio
async def test_execute_cypher_rejects_invalid_graph_name(monkeypatch):
    conn = AsyncMock()
    monkeypatch.setattr(api, "pool", _FakePool(conn))

    req = CypherRequest(
        query="MATCH (v:Memory) RETURN v",
        graph_name="memu_graph'; DROP TABLE memories; --",
    )

    with pytest.raises(HTTPException) as exc:
        await execute_cypher(req=req, _key="memu-dev-key")

    assert exc.value.status_code == 422
    assert "invalid graph_name" in str(exc.value.detail)
    conn.fetch.assert_not_called()
