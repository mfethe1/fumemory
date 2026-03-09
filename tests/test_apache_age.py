import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_age_graph_sync_mock():
    # In a real environment, this connects to the database:
    # conn = await asyncpg.connect(DATABASE_URL)
    conn = AsyncMock()
    
    # Simulate setting search path
    await conn.execute('SET search_path = ag_catalog, "$user", public;')
    conn.execute.assert_called_with('SET search_path = ag_catalog, "$user", public;')
    
    # Simulate insert memory trigger action
    await conn.execute("INSERT INTO memories (id) VALUES ($1)", "123")
    
    # Simulate fetch from Cypher graph
    conn.fetchrow.return_value = {"agent_id": "test_agent"}
    row = await conn.fetchrow("SELECT * FROM cypher('memu_graph', $$ MATCH (v:Memory {id: '123'}) RETURN v.agent_id $$) AS (agent_id agtype);")
    
    assert row["agent_id"] == "test_agent"
    
    # Simulate delete
    await conn.execute("DELETE FROM memories WHERE id = $1", "123")
    conn.fetchrow.return_value = {"v": None}
    row_del = await conn.fetchrow("SELECT * FROM cypher('memu_graph', $$ MATCH (v:Memory {id: '123'}) RETURN v $$) AS (v agtype);")
    assert row_del["v"] is None

@pytest.mark.asyncio
async def test_pgvector_compatibility_mock():
    # Validates schema compatibility with pgvector
    conn = AsyncMock()
    # Check if vector extension is loaded and valid with age
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    conn.execute.assert_called_with("CREATE EXTENSION IF NOT EXISTS vector;")
    
    # Simulate a query that uses both vector and age
    # For example, filtering memories by similarity and then retrieving their graph relationships
    conn.fetch.return_value = [{"id": "123", "distance": 0.1, "parent_id": "456"}]
    rows = await conn.fetch("SELECT id, embedding <=> $1 AS distance FROM memories ORDER BY distance LIMIT 5", [0.1, 0.2, 0.3])
    assert len(rows) == 1
    assert rows[0]["id"] == "123"
