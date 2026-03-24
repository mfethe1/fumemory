"""
Live Apache AGE integration smoke tests.

These tests are opt-in and only run when AGE_TEST_DATABASE_URL is set to a
Postgres instance with Apache AGE enabled. They validate the real DB path for:
- the default memu_graph trigger-backed neighbors traversal
- the custom graph_name neighbors traversal surface
"""
from __future__ import annotations

import os
import pathlib
import sys
import uuid
from uuid import UUID

import asyncpg
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memu import api
from memu.api import get_graph_neighbors


AGE_TEST_DATABASE_URL = os.getenv("AGE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not AGE_TEST_DATABASE_URL,
    reason="Set AGE_TEST_DATABASE_URL to run live Apache AGE integration smoke tests.",
)


class _AcquireCtx:
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _LivePool:
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


@pytest.mark.asyncio
async def test_get_graph_neighbors_live_default_graph_via_sync_trigger(monkeypatch):
    conn = await asyncpg.connect(AGE_TEST_DATABASE_URL)
    monkeypatch.setattr(api, "pool", _LivePool(conn))

    parent_id = UUID("00000000-0000-0000-0000-00000000" + uuid.uuid4().hex[:8])
    child_id = UUID("00000000-0000-0000-0000-00000000" + uuid.uuid4().hex[:8])

    try:
        await conn.execute("LOAD 'age'")
        await conn.execute('SET search_path = ag_catalog, "$user", public')

        await conn.execute(
            """
            INSERT INTO memories (id, content, memory_type, agent_id, metadata, parent_id, confidence, content_hash, embedding)
            VALUES ($1, $2, $3, $4, '{}'::jsonb, NULL, 1.0, $5, NULL)
            ON CONFLICT (id) DO NOTHING
            """,
            parent_id,
            f"AGE integration parent {parent_id}",
            "fact",
            "pytest-age",
            parent_id.hex,
        )
        await conn.execute(
            """
            INSERT INTO memories (id, content, memory_type, agent_id, metadata, parent_id, confidence, content_hash, embedding)
            VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, 1.0, $6, NULL)
            ON CONFLICT (id) DO NOTHING
            """,
            child_id,
            f"AGE integration child {child_id}",
            "fact",
            "pytest-age",
            parent_id,
            child_id.hex,
        )

        result = await get_graph_neighbors(memory_id=child_id, _key="memu-dev-key")

        assert result["ok"] is True
        assert any("DERIVED_FROM" in str(row["rel_type"]) for row in result["neighbors"])
        assert any(str(parent_id) in str(row["neighbor"]) for row in result["neighbors"])
    finally:
        await conn.execute("DELETE FROM memories WHERE id = ANY($1::uuid[])", [child_id, parent_id])
        await conn.close()


@pytest.mark.asyncio
async def test_get_graph_neighbors_live_custom_graph_name(monkeypatch):
    conn = await asyncpg.connect(AGE_TEST_DATABASE_URL)
    monkeypatch.setattr(api, "pool", _LivePool(conn))

    graph_name = f"ageit_{uuid.uuid4().hex[:8]}"
    source_id = str(uuid.uuid4())
    neighbor_id = str(uuid.uuid4())

    try:
        await conn.execute("LOAD 'age'")
        await conn.execute('SET search_path = ag_catalog, "$user", public')
        await conn.execute(f"SELECT create_graph('{graph_name}')")
        await conn.execute(f"SELECT create_vlabel('{graph_name}', 'Memory')")
        await conn.execute(
            f"""
            SELECT * FROM cypher('{graph_name}', $$
                CREATE (:Memory {{id: \"{source_id}\"}})
                CREATE (:Memory {{id: \"{neighbor_id}\"}})
            $$) AS (result agtype)
            """
        )
        await conn.execute(
            f"""
            SELECT * FROM cypher('{graph_name}', $$
                MATCH (a:Memory {{id: \"{source_id}\"}}), (b:Memory {{id: \"{neighbor_id}\"}})
                CREATE (a)-[:RELATED_TO]->(b)
            $$) AS (result agtype)
            """
        )

        result = await get_graph_neighbors(
            memory_id=UUID(source_id),
            graph_name=graph_name,
            _key="memu-dev-key",
        )

        assert result["ok"] is True
        assert any("RELATED_TO" in str(row["rel_type"]) for row in result["neighbors"])
        assert any(neighbor_id in str(row["neighbor"]) for row in result["neighbors"])
    finally:
        try:
            await conn.execute(f"SELECT drop_graph('{graph_name}', true)")
        finally:
            await conn.close()
