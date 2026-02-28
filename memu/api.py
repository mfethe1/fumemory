"""memU API — FastAPI application."""

from __future__ import annotations

import hashlib
import json
import os
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

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
    Task,
    TaskCreate,
    TaskStatus,
)

from memu.notion_bridge import create_bridge_from_env, NotionBridge
from memu.migrations import run_migrations
from memu.temporal_routes import router as temporal_router

# --- Config ---

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://memu:memu@localhost:5432/memu")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "4096"))
DEDUP_THRESHOLD = float(os.environ.get("DEDUP_THRESHOLD", "0.95"))
DECAY_RATE = float(os.environ.get("DECAY_RATE", "0.01"))

# --- Globals ---

pool: asyncpg.Pool | None = None
_fastembed_model: Any = None
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, _fastembed_model
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    
    # Run DB migrations
    try:
        await run_migrations(pool)
    except Exception as e:
        logger.error(f"Migration startup failed: {e}")
    
    # Pre-warm fastembed model
    try:
        from fastembed import TextEmbedding
        _fastembed_model = TextEmbedding()
    except Exception as e:
        logger.warning("FastEmbed pre-warm failed: %s", e)
        
    yield
    if pool:
        await pool.close()


app = FastAPI(
    title="memU",
    description="Free, open-source shared memory for AI agents.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(temporal_router, tags=["Async Workflows"])

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(key: str | None = Security(api_key_header)) -> str:
    if not key or key != MEMU_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


# --- Embedding ---

async def get_embedding(text: str) -> list[float] | None:
    """Get embedding vector from any OpenAI-compatible API (Ollama, OpenAI, etc.).
    Falls back to FastEmbed (local) if API fails, then None if both fail."""

    # 1. Try OpenAI/Ollama API
    if OPENAI_API_KEY or "ollama" in EMBEDDING_BASE_URL:
        url = f"{EMBEDDING_BASE_URL.rstrip('/')}/v1/embeddings"
        headers = {"Content-Type": "application/json"}
        if OPENAI_API_KEY:
            headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    url,
                    headers=headers,
                    json={"input": text, "model": EMBEDDING_MODEL},
                )
                r.raise_for_status()
                emb = r.json()["data"][0]["embedding"]
                if len(emb) == EMBEDDING_DIMS:
                    return emb
                logger.warning("Primary embedding dim mismatch: got %d, expected %d", len(emb), EMBEDDING_DIMS)
        except Exception as e:
            logger.warning("Primary embedding API failed (%s), trying FastEmbed fallback", e)

    # 2. Try FastEmbed (Local fallback)
    global _fastembed_model
    try:
        if _fastembed_model is None:
            from fastembed import TextEmbedding
            _fastembed_model = TextEmbedding()
        
        # fastembed returns a generator
        embeddings = list(_fastembed_model.embed([text]))
        emb = embeddings[0].tolist()
        if len(emb) == EMBEDDING_DIMS:
            return emb
        logger.warning("FastEmbed dim mismatch: got %d, expected %d", len(emb), EMBEDDING_DIMS)
    except Exception as e:
        logger.error("FastEmbed fallback failed: %s", e)
    
    return None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:64]


# --- Helpers ---

def _row_to_memory(row) -> Memory:
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

def _row_to_task(row) -> Task:
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return Task(
        id=row["id"],
        task=row["task"],
        priority=row["priority"],
        status=row["status"],
        owner_id=row["owner_id"],
        lane=row["lane"],
        metadata=metadata,
        evidence=row["evidence"],
        dependency_id=row["dependency_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

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
        # Check for duplicates (content hash always works; vector similarity only when embedding available)
        if embedding is not None:
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
        else:
            existing = await conn.fetchrow(
                "SELECT id, 1.0::float AS similarity FROM memories WHERE content_hash = $1 LIMIT 1",
                c_hash,
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
                json.dumps(req.metadata) if req.metadata else "{}",
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO memories (content, embedding, memory_type, agent_id, metadata, parent_id, confidence, content_hash)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
                RETURNING *
                """,
                req.content,
                str(embedding) if embedding is not None else None,
                req.memory_type.value,
                req.agent_id,
                json.dumps(req.metadata) if req.metadata else "{}",
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
    # Graceful degradation: if embedding service is unavailable, fall back to text search
    if embedding is None:
        logger.warning("Embedding service unavailable for /search, falling back to text search")
        # Delegate to text-based search as fallback
        async with pool.acquire() as conn:
            filters = ["content ILIKE $1"]
            params: list[Any] = [f"%{req.query}%"]
            idx = 2

            if req.agent_id:
                filters.append(f"agent_id = ${idx}")
                params.append(req.agent_id)
                idx += 1
            if req.memory_type:
                filters.append(f"memory_type = ${idx}")
                params.append(req.memory_type.value)
                idx += 1

            where = " AND ".join(filters)
            rows = await conn.fetch(
                f"""
                SELECT *, 0.0::float8 AS similarity
                FROM memories
                WHERE {where}
                ORDER BY updated_at DESC
                LIMIT {req.limit}
                """,
                *params,
            )
        results = []
        for row in rows:
            score = compute_final_score(
                similarity=0.5,  # neutral score for text matches
                created_at=row["created_at"],
                access_count=row["access_count"],
                decay_rate=DECAY_RATE,
                temporal_weight=req.temporal_weight,
            )
            results.append(
                SearchResult(
                    memory=_row_to_memory(row),
                    similarity=0.0,
                    final_score=score,
                )
            )
        results.sort(key=lambda r: r.final_score, reverse=True)
        return results[: req.limit]
    
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
    
    # Audit Trail: Log Search History
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO search_history (query, agent_id, results_count, search_type, metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                req.query,
                req.agent_id or "system",
                len(results),
                "vector",
                json.dumps({"temporal_weight": req.temporal_weight, "limit": req.limit})
            )
    except Exception as e:
        logger.error(f"Failed to log search history: {e}")

    return results[: req.limit]


@app.post("/search-text")
async def search_text(
    query: str,
    agent_id: str | None = None,
    memory_type: str | None = None,
    limit: int = 10,
    _key: str = Depends(verify_api_key),
):
    """
    Full-text search using PostgreSQL ILIKE — no embeddings needed.
    Useful for environments without an embedding provider or for exact keyword lookups.
    """
    filters = ["content ILIKE $1"]
    params: list[Any] = [f"%{query}%"]
    idx = 2

    if agent_id:
        filters.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1
    if memory_type:
        filters.append(f"memory_type = ${idx}")
        params.append(memory_type)
        idx += 1

    where = " AND ".join(filters)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM memories
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT {limit}
            """,
            *params,
        )

    return [_row_to_memory(row) for row in rows]


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, _key: str = Depends(verify_api_key)):
    """RAG chat — retrieves relevant memories and generates an answer."""
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
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    chunk,
                    str(embedding) if embedding else None,
                    req.memory_type.value,
                    req.agent_id,
                    c_hash,
                )
                imported += 1
        except Exception:
            continue

    return BulkImportResponse(imported=imported, duplicates_skipped=dupes)


@app.post("/tasks", response_model=Task)
async def create_task(req: TaskCreate, _key: str = Depends(verify_api_key)):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO backlog (task, priority, owner_id, lane, metadata, dependency_id)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            RETURNING *
            """,
            req.task,
            req.priority.value,
            req.owner_id,
            req.lane,
            json.dumps(req.metadata) if req.metadata else "{}",
            req.dependency_id,
        )
    return _row_to_task(row)


@app.get("/tasks", response_model=list[Task])
async def list_tasks(status: TaskStatus | None = None, owner: str | None = None, _key: str = Depends(verify_api_key)):
    filters = []
    params = []
    idx = 1
    if status:
        filters.append(f"status = ${idx}")
        params.append(status.value)
        idx += 1
    if owner:
        filters.append(f"owner_id = ${idx}")
        params.append(owner)
        idx += 1
    
    where = (" WHERE " + " AND ".join(filters)) if filters else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT * FROM backlog{where} ORDER BY priority ASC, created_at DESC", *params)
    return [_row_to_task(row) for row in rows]


# --- A-MEM Link Layer ---

class LinkCreate(BaseModel):
    source_id: UUID
    target_id: UUID
    relationship: str = "similar"
    strength: float = 0.5


@app.post("/api/v1/memu/links")
async def create_link(req: LinkCreate, _key: str = Depends(verify_api_key)):
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO memory_links (source_id, target_id, relationship, strength)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (source_id, target_id, relationship)
                DO UPDATE SET strength = LEAST(1.0, memory_links.strength + 0.1), last_accessed = NOW()
                RETURNING id, source_id, target_id, relationship, strength
                """,
                req.source_id, req.target_id, req.relationship, req.strength,
            )
            return {"ok": True, "link": dict(row)}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/memu/links/{memory_id}")
async def get_links(memory_id: UUID, _key: str = Depends(verify_api_key)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ml.*, m.content AS linked_content, m.memory_type, m.agent_id
            FROM memory_links ml
            JOIN memories m ON (m.id = CASE WHEN ml.source_id = $1 THEN ml.target_id ELSE ml.source_id END)
            WHERE ml.source_id = $1 OR ml.target_id = $1
            ORDER BY ml.strength DESC
            """,
            memory_id,
        )
        return {"ok": True, "links": [dict(r) for r in rows], "count": len(rows)}


# --- Temporal Queries ---

@app.get("/api/v1/memu/temporal")
async def temporal_search_endpoint(
    q: str,
    agent: str = None,
    hours: float = None,
    limit: int = 10,
    _key: str = Depends(verify_api_key),
):
    """Time-weighted memory search. Recent memories rank higher."""
    embedding = await get_embedding(q)

    filters = ["valid_to IS NULL"]  # only current memories
    params: list[Any] = [str(embedding)]
    idx = 2

    if agent:
        filters.append(f"agent_id = ${idx}")
        params.append(agent)
        idx += 1
    if hours:
        filters.append(f"created_at >= NOW() - INTERVAL '{hours} hours'")

    where = " AND ".join(filters)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT *,
                1 - (embedding <=> $1::vector) AS similarity,
                EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 AS age_days
            FROM memories
            WHERE embedding IS NOT NULL AND {where}
            ORDER BY embedding <=> $1::vector
            LIMIT {limit * 3}
            """,
            *params,
        )

    # Apply time decay reranking (7-day half-life)
    import math
    results = []
    for row in rows:
        age_days = float(row["age_days"])
        base_sim = float(row["similarity"])
        decay = math.exp(-0.693 * age_days / 7.0)
        temporal_score = base_sim * decay
        results.append({
            "id": str(row["id"]),
            "content": row["content"],
            "agent_id": row["agent_id"],
            "memory_type": row["memory_type"],
            "similarity": base_sim,
            "temporal_score": temporal_score,
            "age_days": round(age_days, 1),
            "created_at": str(row["created_at"]),
        })

    results.sort(key=lambda r: r["temporal_score"], reverse=True)
    return {"ok": True, "results": results[:limit]}


# --- Point-in-Time Query ---

@app.get("/api/v1/memu/at")
async def point_in_time_query(
    timestamp: str,
    agent: str = None,
    limit: int = 20,
    _key: str = Depends(verify_api_key),
):
    """Query memories as they existed at a specific point in time."""
    from datetime import datetime as dt

    try:
        point = dt.fromisoformat(timestamp)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp format. Use ISO-8601.")

    filters = ["valid_from <= $1", "(valid_to IS NULL OR valid_to > $1)"]
    params: list[Any] = [point]
    idx = 2

    if agent:
        filters.append(f"agent_id = ${idx}")
        params.append(agent)
        idx += 1

    where = " AND ".join(filters)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, memory_type, agent_id, confidence, created_at, valid_from, valid_to
            FROM memories
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT {limit}
            """,
            *params,
        )

    return {"ok": True, "point_in_time": timestamp, "memories": [dict(r) for r in rows], "count": len(rows)}




# --- Notion Integration ---


class NotionClaimRequest(BaseModel):
    task_id: str
    agent_id: str


class NotionCompleteRequest(BaseModel):
    task_id: str
    agent_id: str
    notes: str
    memory_type: str = "lesson"


class NotionCreateRequest(BaseModel):
    title: str
    priority: str = "P2"
    project: str = ""
    assigned_agent: str = "Any"


async def get_notion_bridge() -> NotionBridge:
    return await create_bridge_from_env()


@app.get("/notion/queue")
async def notion_queue(agent_id: str | None = None, _key: str = Depends(verify_api_key)):
    bridge = await get_notion_bridge()
    try:
        return await bridge.poll_tasks(agent_id=agent_id)
    finally:
        await bridge.close()


@app.post("/notion/claim")
async def notion_claim(req: NotionClaimRequest, _key: str = Depends(verify_api_key)):
    bridge = await get_notion_bridge()
    try:
        return await bridge.claim_task(req.task_id, req.agent_id)
    finally:
        await bridge.close()


@app.post("/notion/complete")
async def notion_complete(req: NotionCompleteRequest, _key: str = Depends(verify_api_key)):
    bridge = await get_notion_bridge()
    try:
        return await bridge.complete_task(req.task_id, req.agent_id, req.notes, req.memory_type)
    finally:
        await bridge.close()


@app.post("/notion/create")
async def notion_create(req: NotionCreateRequest, _key: str = Depends(verify_api_key)):
    bridge = await get_notion_bridge()
    try:
        task_id = await bridge.create_task(
            title=req.title,
            priority=req.priority,
            project=req.project,
            assigned_agent=req.assigned_agent,
        )
        return {"task_id": task_id}
    finally:
        await bridge.close()


@app.get("/notion/health")
async def notion_health(_key: str = Depends(verify_api_key)):
    bridge = await get_notion_bridge()
    try:
        return await bridge.health_check()
    finally:
        await bridge.close()



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
