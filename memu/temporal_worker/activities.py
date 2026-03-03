# memu/temporal_worker/activities.py
import json
import os
from typing import Any

import asyncpg
from temporalio import activity
import httpx

# Local cache for FastEmbed fallback (kept module-private for worker determinism)
_fastembed_model: Any | None = None


def get_db_url():
    return os.environ.get("DATABASE_URL")


# --- Embedding helpers (OpenAI-compatible + local fallback) ---

EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "4096"))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


async def _embedding_from_http(text: str) -> list[float] | None:
    """Try OpenAI-compatible endpoint (/v1/embeddings), including Ollama compatibility."""
    # Only attempt remote embedding call for explicit providers.
    if not OPENAI_API_KEY and "ollama" not in EMBEDDING_BASE_URL:
        return None

    base = EMBEDDING_BASE_URL.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

    async def _try(url: str, payload: dict) -> list[float] | None:
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                if "data" in data and data["data"]:
                    emb = data["data"][0].get("embedding")
                    if emb is not None:
                        return emb
                if "embedding" in data:
                    return data["embedding"]
        except Exception as e:
            activity.logger.warning("Remote embedding request failed (%s): %s", url, e)
        return None

    emb = await _try(
        f"{base}/v1/embeddings",
        {
            "input": text,
            "model": EMBEDDING_MODEL,
            "dimensions": EMBEDDING_DIMS,
        },
    )
    if emb is not None and len(emb) == EMBEDDING_DIMS:
        return emb

    emb = await _try(
        f"{base}/api/embeddings",
        {
            "model": EMBEDDING_MODEL,
            "prompt": text,
        },
    )
    if emb is not None and len(emb) == EMBEDDING_DIMS:
        return emb
    if emb is not None:
        activity.logger.warning(
            "Embedding dim mismatch from remote provider: got=%d expected=%d",
            len(emb),
            EMBEDDING_DIMS,
        )

    return None


async def _embedding_from_fastembed(text: str) -> list[float] | None:
    global _fastembed_model

    if _fastembed_model is None:
        try:
            from fastembed import TextEmbedding

            _fastembed_model = TextEmbedding()
        except Exception as e:
            activity.logger.warning("FastEmbed unavailable: %s", e)
            return None

    try:
        embeddings = list(_fastembed_model.embed([text]))
        emb = embeddings[0].tolist()
        if len(emb) == EMBEDDING_DIMS:
            return emb
        activity.logger.warning(
            "FastEmbed dim mismatch: got=%d expected=%d",
            len(emb),
            EMBEDDING_DIMS,
        )
    except Exception as e:
        activity.logger.warning("FastEmbed generation failed: %s", e)
    return None


@activity.defn
async def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding vector (activity wrapper)."""
    # Mirror API behavior: prefer remote embedding API, then local FastEmbed fallback.
    remote = await _embedding_from_http(text)
    if remote is not None:
        return remote
    return await _embedding_from_fastembed(text)


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
            content,
            agent_id,
            json.dumps(metadata),
            str(embedding) if embedding else None,
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
                str(embedding),
                agent_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, content, 0.0 as similarity FROM memories WHERE content ILIKE $1 AND agent_id = $2 LIMIT 5",
                f"%{query}%",
                agent_id,
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
            action_type,
            agent_id,
            json.dumps(details),
        )
        return True
    finally:
        await conn.close()


# Export for worker registration
GenerateEmbeddingActivity = generate_embedding
StoreMemoryActivity = store_memory
SearchMemoryActivity = search_memory
LogAuditActivity = log_audit
