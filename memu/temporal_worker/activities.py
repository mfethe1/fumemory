import asyncio
import os
import asyncpg
import json
from temporalio import activity
from memu.models import MemoryCreate

@activity.defn
async def store_memory(content: str, agent_id: str, metadata: dict) -> str:
    """Store memory and return ID."""
    db_url = os.environ.get("DATABASE_URL")
    conn = await asyncpg.connect(db_url)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, agent_id, metadata, memory_type)
            VALUES ($1, $2, $3, 'user_action')
            RETURNING id
            """,
            content, agent_id, json.dumps(metadata)
        )
        return str(row["id"])
    finally:
        await conn.close()

@activity.defn
async def search_memory(query: str, agent_id: str) -> list:
    """Execute search query."""
    db_url = os.environ.get("DATABASE_URL")
    conn = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            "SELECT id, content FROM memories WHERE content ILIKE $1 AND agent_id = $2 LIMIT 5",
            f"%{query}%", agent_id
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@activity.defn
async def log_audit(action_type: str, agent_id: str, details: dict):
    """Log structured audit event."""
    db_url = os.environ.get("DATABASE_URL")
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute(
            """
            INSERT INTO audit_log (action_type, agent_id, details, created_at)
            VALUES ($1, $2, $3, NOW())
            """,
            action_type, agent_id, json.dumps(details)
        )
        return True
    finally:
        await conn.close()
