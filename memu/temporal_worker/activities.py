# memu/temporal_worker/activities.py
import asyncio
import os
import asyncpg
import json
from temporalio import activity
import httpx

# Helper to get DB connection
def get_db_url():
    return os.environ.get("DATABASE_URL")

@activity.defn
async def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding vector (activity wrapper)."""
    # This logic should mirror api.py's get_embedding
    # For now, simplistic call to Ollama or OpenAI
    base_url = os.environ.get("EMBEDDING_BASE_URL", "http://ollama:11434")
    model = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding")
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base_url}/api/embeddings",
                json={"model": model, "prompt": text}
            )
            if resp.status_code == 200:
                return resp.json().get("embedding")
    except Exception as e:
        activity.logger.error(f"Embedding failed: {e}")
    return None

@activity.defn
async def store_memory(content: str, agent_id: str, metadata: dict, embedding: list[float] | None) -> str:
    """Store memory and return ID."""
    conn = await asyncpg.connect(get_db_url())
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO memories (content, agent_id, metadata, memory_type, embedding)
            VALUES ($1, $2, $3, 'user_action', $4::vector)
            RETURNING id
            """,
            content, agent_id, json.dumps(metadata), str(embedding) if embedding else None
        )
        return str(row["id"])
    finally:
        await conn.close()

@activity.defn
async def search_memory(query: str, agent_id: str, embedding: list[float] | None) -> list:
    """Execute search query."""
    conn = await asyncpg.connect(get_db_url())
    try:
        if embedding:
            rows = await conn.fetch(
                """
                SELECT id, content, 1 - (embedding <=> $1::vector) as similarity
                FROM memories 
                WHERE agent_id = $2
                ORDER BY embedding <=> $1::vector
                LIMIT 5
                """,
                str(embedding), agent_id
            )
        else:
            rows = await conn.fetch(
                "SELECT id, content, 0.0 as similarity FROM memories WHERE content ILIKE $1 AND agent_id = $2 LIMIT 5",
                f"%{query}%", agent_id
            )
        return [dict(r) for r in rows]
    finally:
        await conn.close()

@activity.defn
async def log_audit(action_type: str, agent_id: str, details: dict):
    """Log structured audit event."""
    conn = await asyncpg.connect(get_db_url())
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

# Export for worker registration
GenerateEmbeddingActivity = generate_embedding
StoreMemoryActivity = store_memory
SearchMemoryActivity = search_memory
LogAuditActivity = log_audit
