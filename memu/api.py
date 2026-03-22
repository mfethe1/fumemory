"""memU API — FastAPI application."""

from __future__ import annotations

import hashlib
import json
import os
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from memu.decay import compute_final_score, should_deduplicate
from memu.agent_events_consumer import AgentEventsConsumer
from memu.lane_lock import STATE_EVENTS_ENABLED, get_state_event_evidence
from memu.state_projector import StateProjectorConsumer
from memu.models import (
    BulkImportRequest,
    BulkImportResponse,
    ChatRequest,
    ChatResponse,
    Memory,
    MemoryCreate,
    MemoryType,
    SearchRequest,
    SearchResult,
)

# --- Config ---

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://memu:memu@localhost:5432/memu")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "4096"))
DEDUP_THRESHOLD = float(os.environ.get("DEDUP_THRESHOLD", "0.95"))
DECAY_RATE = float(os.environ.get("DECAY_RATE", "0.01"))

logger = logging.getLogger(__name__)

# --- Globals ---

pool: asyncpg.Pool | None = None
agent_events_consumer: AgentEventsConsumer | None = None
state_projector_consumer: StateProjectorConsumer | None = None
ENABLE_AGENT_EVENTS_CONSUMER = os.environ.get("ENABLE_AGENT_EVENTS_CONSUMER", "true").lower() == "true"
ENABLE_STATE_PROJECTOR = os.environ.get("ENABLE_STATE_PROJECTOR", "true").lower() == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    global agent_events_consumer
    global state_projector_consumer

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

    # Run schema migration 002 (idempotent — IF NOT EXISTS throughout)
    try:
        migration_path = os.path.join(os.path.dirname(__file__), "migrations", "002_memory_enhancement.sql")
        if os.path.exists(migration_path):
            with open(migration_path) as f:
                migration_sql = f.read()
            async with pool.acquire() as conn:
                # Run statement by statement to handle partial failures gracefully
                for stmt in migration_sql.split(";"):
                    stmt = stmt.strip()
                    if stmt and not stmt.startswith("--"):
                        try:
                            await conn.execute(stmt)
                        except Exception as stmt_err:
                            logger.warning("Migration stmt skipped: %s — %s", stmt[:80], stmt_err)
            logger.info("Schema migration 002 applied (idempotent)")
    except Exception as e:
        logger.warning("Migration 002 failed (non-fatal): %s", e)

    # Wire enhanced search router
    try:
        from memu.search_enhanced import router as enhanced_router, configure as configure_enhanced
        configure_enhanced(pool, get_embedding, verify_api_key)
        app.include_router(enhanced_router)
        logger.info("Enhanced search endpoints registered (/search/hybrid, /search/raptor, /search/grep)")
    except Exception as e:
        logger.warning("Enhanced search router failed to load (non-fatal): %s", e)

    if ENABLE_AGENT_EVENTS_CONSUMER:
        try:
            agent_events_consumer = AgentEventsConsumer()
            await agent_events_consumer.start()
        except Exception as e:
            logger.warning("AGENT_EVENTS durable consumer startup failed: %s", e)
            agent_events_consumer = None

    if ENABLE_STATE_PROJECTOR:
        try:
            state_projector_consumer = StateProjectorConsumer(pool=pool)
            await state_projector_consumer.start()
        except Exception as e:
            logger.warning("STATE_PROJECTOR durable consumer startup failed: %s", e)
            state_projector_consumer = None

    try:
        yield
    finally:
        if agent_events_consumer:
            await agent_events_consumer.stop()
        if state_projector_consumer:
            await state_projector_consumer.stop()
        if pool:
            await pool.close()


app = FastAPI(
    title="memU",
    description="Free, open-source shared memory for AI agents.",
    version="0.1.0",
    lifespan=lifespan,
)

memu_key_header = APIKeyHeader(name="X-MemU-Key", auto_error=False)
legacy_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(
    memu_key: str | None = Security(memu_key_header),
    legacy_key: str | None = Security(legacy_api_key_header),
) -> str:
    key = memu_key or legacy_key
    if not key:
        raise HTTPException(status_code=401, detail="Missing authentication credentials")
    if key != MEMU_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid X-MemU-Key")
    return key


# --- Embedding ---

async def get_embedding(text: str) -> list[float]:
    """Get embedding vector from any OpenAI-compatible API (Ollama, OpenAI, etc.)."""
    import httpx

    url = f"{EMBEDDING_BASE_URL.rstrip('/')}/v1/embeddings"
    headers = {"Content-Type": "application/json"}
    if OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            url,
            headers=headers,
            json={"input": text, "model": EMBEDDING_MODEL},
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:64]


def _ensure_memory_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(metadata or {})
    payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
    return payload


def _extract_entities(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", text or "")}


def _entity_overlap_score(query: str, content: str) -> float:
    q_entities = _extract_entities(query)
    if not q_entities:
        return 0.0
    c_entities = _extract_entities(content)
    if not c_entities:
        return 0.0
    return len(q_entities & c_entities) / len(q_entities)


# --- Routes ---

@app.get("/health")
async def health():
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    report = {"status": "healthy", "version": "0.1.0"}
    if agent_events_consumer:
        report["agent_events"] = await _agent_events_report()
    if state_projector_consumer:
        report["state_projector"] = await _state_projector_report()
    return report


@app.get("/api/v1/memu/health")
async def memu_health_compat():
    return await health()


@app.get("/health/report")
async def health_report():
    """Health artifact for durable AGENT_EVENTS consumer metrics."""
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        status = "healthy"
    except Exception:
        status = "degraded"

    return {
        "artifact": "agent_events",
        "status": status,
        "version": "0.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent_events": await _agent_events_report(),
        "state_projector": await _state_projector_report(),
    }


@app.get("/state-events/evidence")
async def state_events_evidence():
    """Runtime artifact showing emitted STATE_EVENTS sequence and projector-applied sequence."""
    emitter_tail = get_state_event_evidence(limit=50)

    projector_tail = []
    table_tail = []
    status = "disabled"

    if state_projector_consumer:
        status = "enabled"
        projector_tail = state_projector_consumer.applied_event_trace()

        if pool:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT event_id, gateway_id, agent_id, event_type, entity_id,
                           version, applied_at
                    FROM state_events
                    ORDER BY applied_at DESC, version DESC
                    LIMIT 50
                    """
                )
                table_tail = [
                    {
                        "event_id": str(row["event_id"]),
                        "gateway_id": row["gateway_id"],
                        "agent_id": row["agent_id"],
                        "event_type": row["event_type"],
                        "entity_id": row["entity_id"],
                        "version": row["version"],
                        "applied_at": row["applied_at"].isoformat(),
                    }
                    for row in rows
                ]

    return {
        "artifact": "state_events_runtime",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "state_events_enabled": STATE_EVENTS_ENABLED,
        "emitter": {
            "recent": emitter_tail,
            "count": len(emitter_tail),
        },
        "state_projector": {
            "status": status,
            "recent_applied": projector_tail,
            "table_recent": table_tail,
        },
    }


async def _agent_events_report() -> dict[str, Any]:
    if not agent_events_consumer:
        return {
            "enabled": False,
            "status": "disabled",
        }

    artifact = await agent_events_consumer.health_artifact()
    return {
        "enabled": True,
        **artifact,
    }


async def _state_projector_report() -> dict[str, Any]:
    if not state_projector_consumer:
        return {
            "enabled": False,
            "status": "disabled",
        }

    artifact = await state_projector_consumer.health_artifact()
    return {
        "enabled": True,
        "status": "healthy",
        **artifact,
    }


def _warn_if_missing_quality_tags(req: MemoryCreate) -> None:
    quality = req.metadata.get("quality") if isinstance(req.metadata, dict) else None
    if not isinstance(quality, dict):
        logger.warning("memory.create missing metadata.quality tags (confidence/supersedes/expires)")
        return

    missing = [k for k in ("confidence", "supersedes", "expires") if k not in quality]
    if missing:
        logger.warning("memory.create missing metadata.quality keys: %s", ", ".join(missing))


@app.post("/memories", response_model=Memory)
async def create_memory(req: MemoryCreate, _key: str = Depends(verify_api_key)):
    _warn_if_missing_quality_tags(req)
    normalized_metadata = _ensure_memory_metadata(req.metadata)
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
                json.dumps(normalized_metadata),
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
                json.dumps(normalized_metadata),
                req.parent_id,
                req.confidence,
                c_hash,
            )

    return _row_to_memory(row)


class MemUAddCompatRequest(BaseModel):
    content: str | None = None
    text: str | None = None
    agent_id: str = "unknown"
    memory_type: MemoryType = MemoryType.FACT
    metadata: dict[str, Any] = {}
    confidence: float = 1.0


@app.post("/api/v1/memu/add")
async def memu_add_compat(req: MemUAddCompatRequest, _key: str = Depends(verify_api_key)):
    if req.content is None and req.text is not None:
        return {"ok": False, "error_code": "INVALID_PAYLOAD", "message": "Use content instead of text"}
    if req.content is None or not req.content.strip():
        return {"ok": False, "error_code": "MISSING_CONTENT", "message": "Missing required field: content"}

    created = await create_memory(
        MemoryCreate(
            content=req.content,
            memory_type=req.memory_type,
            agent_id=req.agent_id,
            metadata=req.metadata,
            confidence=req.confidence,
        ),
        _key=_key,
    )
    return {"ok": True, "id": str(created.id), "message": "Memory stored"}



@app.get("/memories/recent", response_model=list[Memory])
async def get_recent_memories(
    limit: int = 10,
    agent_id: str | None = None,
    memory_type: str | None = None,
    _key: str = Depends(verify_api_key)
):
    query = "SELECT * FROM memories "
    conditions = []
    args = []
    
    if agent_id:
        args.append(agent_id)
        conditions.append(f"agent_id = ${len(args)}")
    if memory_type:
        args.append(memory_type)
        conditions.append(f"memory_type = ${len(args)}")
        
    if conditions:
        query += "WHERE " + " AND ".join(conditions)
        
    args.append(limit)
    query += f" ORDER BY created_at DESC LIMIT ${len(args)}"
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
        
    return [_row_to_memory(row) for row in rows]

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


def _quality_tags_from_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    quality = metadata.get("quality")
    return quality if isinstance(quality, dict) else {}


def _parse_quality_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _quality_confidence_penalty(metadata: Any) -> float:
    quality = _quality_tags_from_metadata(metadata)
    if quality.get("confidence") == "low":
        return 0.65
    return 1.0


def _apply_retrieval_safety_policy(rows: Iterable[Any], *, now: datetime | None = None) -> list[Any]:
    now = now or datetime.now(timezone.utc)
    candidate_rows = [dict(row) for row in rows]

    superseded_ids: set[str] = set()
    for row in candidate_rows:
        quality = _quality_tags_from_metadata(row.get("metadata"))
        superseded = quality.get("supersedes")
        if isinstance(superseded, str) and superseded:
            superseded_ids.add(superseded)

    filtered: list[Any] = []
    for row in candidate_rows:
        metadata = row.get("metadata")
        quality = _quality_tags_from_metadata(metadata)

        if isinstance(metadata, dict) and metadata.get("source") == "untrusted":
            continue

        expires_at = _parse_quality_expiry(quality.get("expires"))
        if expires_at is not None and expires_at <= now:
            continue

        row_id = str(row.get("id"))
        if row_id in superseded_ids:
            continue

        filtered.append(row)

    return filtered


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
    if req.time_window_start:
        filters.append(f"created_at >= ${idx}")
        params.append(req.time_window_start)
        idx += 1
    if req.time_window_end:
        filters.append(f"created_at <= ${idx}")
        params.append(req.time_window_end)
        idx += 1

    where = (" AND " + " AND ".join(filters)) if filters else ""

    async with pool.acquire() as conn:
        candidate_limit = max(req.limit * 8, 32)
        rows = await conn.fetch(
            f"""
            WITH candidates AS (
                SELECT *
                FROM memories
                WHERE embedding IS NOT NULL{where}
                ORDER BY ((binary_quantize(embedding))::bit(4096)) <~> ((binary_quantize($1::vector))::bit(4096))
                LIMIT {candidate_limit}
            )
            SELECT *, 1 - (embedding <=> $1::vector) AS similarity
            FROM candidates
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

    policy_rows = _apply_retrieval_safety_policy(rows)

    # Apply decay scoring and Graphiti-style entity overlap blend.
    filtered_rows = []
    now = datetime.now(timezone.utc)
    for row in policy_rows:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        stale_after_raw = metadata.get("stale_after")
        if isinstance(stale_after_raw, str):
            try:
                stale_after = datetime.fromisoformat(stale_after_raw.replace("Z", "+00:00"))
                if stale_after.tzinfo is None:
                    stale_after = stale_after.replace(tzinfo=timezone.utc)
                if stale_after <= now:
                    continue
            except ValueError:
                pass
        filtered_rows.append(row)

    results = []
    for row in filtered_rows:
        score = compute_final_score(
            similarity=row["similarity"],
            created_at=row["created_at"],
            access_count=row["access_count"],
            decay_rate=DECAY_RATE,
            temporal_weight=req.temporal_weight,
        )
        score *= _quality_confidence_penalty(row.get("metadata"))
        entity_overlap = _entity_overlap_score(req.query, row["content"])
        score = (1 - req.entity_weight) * score + (req.entity_weight * entity_overlap)
        results.append(
            SearchResult(
                memory=_row_to_memory(row),
                similarity=row["similarity"],
                final_score=score,
            )
        )

    results.sort(key=lambda r: r.final_score, reverse=True)
    return results[: req.limit]


@app.post("/api/v1/memu/search")
async def memu_search_compat(req: SearchRequest, _key: str = Depends(verify_api_key)):
    return await search_memories(req, _key=_key)


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
            LIMIT {limit * 3}
            """,
            *params,
        )

    policy_rows = _apply_retrieval_safety_policy(rows)
    policy_rows.sort(
        key=lambda row: (row["updated_at"], row["confidence"] * _quality_confidence_penalty(row.get("metadata"))),
        reverse=True,
    )
    return [_row_to_memory(row) for row in policy_rows[:limit]]


class MemUSearchTextCompatRequest(BaseModel):
    query: str
    agent_id: str | None = None
    memory_type: str | None = None
    limit: int = 10


@app.post("/api/v1/memu/search-text")
async def memu_search_text_compat(req: MemUSearchTextCompatRequest, _key: str = Depends(verify_api_key)):
    return await search_text(
        query=req.query,
        agent_id=req.agent_id,
        memory_type=req.memory_type,
        limit=req.limit,
        _key=_key,
    )


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
