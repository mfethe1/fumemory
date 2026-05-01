from __future__ import annotations

import pathlib
import sys
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memu import api


async def _fake_recall_search(query=None, q=None, limit=5, agent_id=None, _key=None):
    return {
        "query": query or q,
        "limit": limit,
        "agent_id": agent_id,
        "key": _key,
    }


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


class _FakeTenantConn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_recall_routes_accept_post_payload(monkeypatch):
    monkeypatch.setattr(api, "recall_search", _fake_recall_search)
    api.app.dependency_overrides[api.verify_api_key] = lambda: "memu-dev-key"

    try:
        client = TestClient(api.app)

        local = client.post("/search/recall", json={"query": "heartbeat", "limit": 1, "agent_id": "lenny"})
        compat = client.post("/api/v1/memu/search/recall", json={"query": "heartbeat", "limit": 2, "agent": "rosie"})

        assert local.status_code == 200
        assert local.json() == {
            "query": "heartbeat",
            "limit": 1,
            "agent_id": "lenny",
            "key": "memu-dev-key",
        }

        assert compat.status_code == 200
        assert compat.json() == {
            "query": "heartbeat",
            "limit": 2,
            "agent_id": "rosie",
            "key": "memu-dev-key",
        }
    finally:
        api.app.dependency_overrides.pop(api.verify_api_key, None)


@pytest.mark.asyncio
async def test_recall_search_falls_back_to_text_when_embedding_unavailable(monkeypatch):
    conn = AsyncMock()
    conn.fetch.return_value = [
        {
            "query": "heartbeat",
            "agent_id": "lenny",
            "created_at": "2026-03-25T00:00:00Z",
            "results_count": 1,
            "similarity": 0.0,
        }
    ]

    monkeypatch.setattr(api, "pool", _FakePool(conn))
    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_tenant_conn", lambda _auth: _FakeTenantConn(conn))

    result = await api.recall_search(query="heartbeat", limit=1, _key="memu-dev-key")

    assert result == [
        {
            "query": "heartbeat",
            "agent_id": "lenny",
            "created_at": "2026-03-25T00:00:00Z",
            "results_count": 1,
            "similarity": 0.0,
        }
    ]
    fetch_sql = conn.fetch.await_args.args[0]
    assert "query ILIKE $1" in fetch_sql
    assert conn.fetch.await_args.args[1] == "%heartbeat%"
    assert conn.fetch.await_args.args[2] == 1
