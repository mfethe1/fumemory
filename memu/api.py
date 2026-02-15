"""memU API — FastAPI application."""

from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader

from memu.decay import compute_final_score, should_deduplicate
from memu.models import (
    BulkImportRequest,
    BulkImportResponse,
    ChatRequest,
    ChatResponse,
    Memory,
    MemoryCreate,
    SearchRequest,
    SearchResult,
)

# --- Config ---

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://memu:memu@localhost:5432/memu")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "1536"))
DEDUP_THRESHOLD = float(os.environ.get("DEDUP_THRESHOLD", "0.95"))
DECAY_RATE = float(os.environ.get("DECAY_RATE", "0.01"))

# --- Globals ---

pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    yield
    if pool:
        await pool.close()


app = FastAPI(
    title="memU",
    description="Free, open-source shared memory for AI agents.",
    version="0.1.0",
    lifespan=lifespan,
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str | None = Security(api_key_header)) -> str:
    if not key or key != MEMU_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


# --- Embedding ---

async def get_embedding(text: str) -> list[float]:
    """Get embedding vector from OpenAI-compatible API."""
    import httpx

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"input": text, "model": EMBEDDING_MODEL},
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:64]


# --- Routes ---

@app.get("/health")
async def health():
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "healthy", "version": "0.1.0"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/memories", response_model=Memory)
async def create_memory(req: MemoryCreate, _key: str = Depends(verify_api_key)):
    embedding = await get_embedding(req.content)
    c_hash = content_hash(req.content)

    async with pool.acquire() as conn:
        # Check for duplicates
        existing = await conn.fetchrow(
            """
            SELECT id, 1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE content_hash = $2 OR (1 - (embedding <=> $1::vector)) > $3
            ORDER BY similarity DESC
            LIMIT 1
            """,
            str(embedding),
            c_hash,
            DEDUP_THRESHOLD,
        )

        if existing and should_deduplicate(existing["similarity"], DEDUP_THRESHOLD):
            # Update existing memory instead of duplicating
            row = await conn.fetchrow(
                """
                UPDATE memories SET
                    access_count = access_count + 1,
                    confidence = GREATEST(confidence, $2),
                    metadata = metadata || $3::jsonb,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                existing["id"],
                req.confidence,
                str(req.metadata) if req.metadata else "{}",
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO memories (content, embedding, memory_type, agent_id, metadata, parent_id, confidence, content_hash)
                VALUES ($1, $2::vector, $3, $4, $5::jsonb, $6, $7, $8)
                RETURNING *
                """,
                req.content,
                str(embedding),
                req.memory_type.value,
                req.agent_id,
                str(req.metadata) if req.metadata else "{}",
                req.parent_id,
                req.confidence,
                c_hash,
            )

    return _row_to_memory(row)


@app.get("/memories/{memory_id}", response_model=Memory)
async def get_memory(memory_id: UUID, _key: str = Depends(verify_api_key)):
    async with pool.acquire() as conn:
        # Increment access count (reinforcement)
        row = await conn.fetchrow(
            """
            UPDATE memories SET access_count = access_count + 1
            WHERE id = $1
            RETURNING *
            """,
            memory_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")
    return _row_to_memory(row)


@app.delete("/memories/{memory_id}")
async def delete_memory(memory_id: UUID, _key: str = Depends(verify_api_key)):
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True}


@app.post("/search", response_model=list[SearchResult])
async def search_memories(req: SearchRequest, _key: str = Depends(verify_api_key)):
    embedding = await get_embedding(req.query)

    filters = []
    params: list[Any] = [str(embedding)]
    idx = 2

    if req.agent_id:
        filters.append(f"agent_id = ${idx}")
        params.append(req.agent_id)
        idx += 1
    if req.memory_type:
        filters.append(f"memory_type = ${idx}")
        params.append(req.memory_type.value)
        idx += 1
    if req.min_confidence > 0:
        filters.append(f"confidence >= ${idx}")
        params.append(req.min_confidence)
        idx += 1

    where = (" AND " + " AND ".join(filters)) if filters else ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT *, 1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE embedding IS NOT NULL{where}
            ORDER BY embedding <=> $1::vector
            LIMIT {req.limit * 3}
            """,
            *params,
        )

        # Increment access counts for returned results
        if rows:
            ids = [r["id"] for r in rows]
            await conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ANY($1)",
                ids,
            )

    # Apply decay scoring and re-rank
    results = []
    for row in rows:
        score = compute_final_score(
            similarity=row["similarity"],
            created_at=row["created_at"],
            access_count=row["access_count"],
            decay_rate=DECAY_RATE,
            temporal_weight=req.temporal_weight,
        )
        results.append(
            SearchResult(
                memory=_row_to_memory(row),
                similarity=row["similarity"],
                final_score=score,
            )
        )

    results.sort(key=lambda r: r.final_score, reverse=True)
    return results[: req.limit]


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, _key: str = Depends(verify_api_key)):
    """RAG chat — retrieves relevant memories and generates an answer."""
    import httpx

    # Search for relevant context
    search_req = SearchRequest(query=req.question, limit=req.context_limit, agent_id=req.agent_id)
    search_results = await search_memories(search_req, _key="internal")

    context = "\n\n".join(
        f"[{r.memory.memory_type.value}] {r.memory.content}" for r in search_results
    )

    # Generate answer using OpenAI-compatible chat
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": "You answer questions based on the provided memory context. Be concise and accurate. Cite memory types when relevant.",
                    },
                    {
                        "role": "user",
                        "content": f"Context from memory:\n{context}\n\nQuestion: {req.question}",
                    },
                ],
                "max_tokens": 1000,
            },
        )
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"]

    return ChatResponse(answer=answer, sources=[r.memory for r in search_results])


@app.post("/memories/bulk", response_model=BulkImportResponse)
async def bulk_import(req: BulkImportRequest, _key: str = Depends(verify_api_key)):
    """Bulk import memories from text, split by delimiter."""
    chunks = [c.strip() for c in req.content.split(req.split_on) if c.strip()]

    imported = 0
    dupes = 0

    for chunk in chunks:
        try:
            mem_req = MemoryCreate(
                content=chunk,
                memory_type=req.memory_type,
                agent_id=req.agent_id,
            )
            embedding = await get_embedding(chunk)
            c_hash = content_hash(chunk)

            async with pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT id FROM memories WHERE content_hash = $1", c_hash
                )
                if existing:
                    dupes += 1
                    continue

                await conn.execute(
                    """
                    INSERT INTO memories (content, embedding, memory_type, agent_id, content_hash)
                    VALUES ($1, $2::vector, $3, $4, $5)
                    """,
                    chunk,
                    str(embedding),
                    req.memory_type.value,
                    req.agent_id,
                    c_hash,
                )
                imported += 1
        except Exception:
            continue

    return BulkImportResponse(imported=imported, duplicates_skipped=dupes)


# --- Helpers ---

def _row_to_memory(row) -> Memory:
    import json

    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    return Memory(
        id=row["id"],
        content=row["content"],
        memory_type=row["memory_type"],
        agent_id=row["agent_id"],
        metadata=metadata,
        parent_id=row["parent_id"],
        confidence=row["confidence"],
        access_count=row["access_count"],
        decay_score=row["decay_score"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
