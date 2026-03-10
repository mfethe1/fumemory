"""memU API â€” FastAPI application."""

from __future__ import annotations
from datetime import datetime, timezone

import hashlib
import json
from memu.web_search_ingest import ingest_web_search
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
from memu.retrieval import (
    blend_hybrid_score,
    compute_graph_temporal_boost,
    normalize_ranked_rows,
    reciprocal_rank_fusion,
)

# --- Logging Config ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
# Reduce noise from verbose libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("fastembed").setLevel(logging.WARNING)
from memu.cluster import NATSClusterManager
from memu.nats_publisher import NATSEventPublisher
from memu.models import (
    BulkImportRequest,
    BulkImportResponse,
    ChatRequest,
    ChatResponse,
    Memory,
    MemoryBlock,
    MemoryBlockCreate,
    MemoryCreate,
    MemoryType,
    SearchRequest,
    SearchResult,
    Task,
    TaskCreate,
    TaskStatus,
    TaskUpdate,
)

from memu.notion_bridge import create_bridge_from_env, NotionBridge
from memu.migrations import run_migrations
from memu.temporal_routes import router as temporal_router
from memu.tenancy import (
    resolve_tenant_id,
    tenant_connection,
    tenant_transaction,
    DEFAULT_TENANT_ID,
    SINGLE_TENANT_MODE,
)

# --- Config ---

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://memu:memu@localhost:5432/memu")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "384"))
DEDUP_THRESHOLD = float(os.environ.get("DEDUP_THRESHOLD", "0.95"))
DECAY_RATE = float(os.environ.get("DECAY_RATE", "0.01"))
STATUS_FRESHNESS_MAX_AGE_MINUTES = int(os.environ.get("STATUS_FRESHNESS_MAX_AGE_MINUTES", "240"))
SECRET_HYGIENE_ENABLED = os.environ.get("MEMU_SECRET_HYGIENE", "true").lower() in ("1", "true", "yes")
HISTORY_HYGIENE_ENABLED = os.environ.get("MEMU_HISTORY_HYGIENE", "true").lower() in ("1", "true", "yes")

# --- Globals ---

pool: asyncpg.Pool | None = None
_fastembed_model: Any = None
_nats_cluster: NATSClusterManager | None = None
_nats_publisher: NATSEventPublisher | None = None
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, _fastembed_model, _nats_cluster, _nats_publisher
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

    # Connect NATS cluster (non-blocking â€” API works without NATS)
    try:
        _nats_cluster = NATSClusterManager()
        await _nats_cluster.connect()
        _nats_publisher = NATSEventPublisher(_nats_cluster, gateway_id="memu-api")
        logger.info("NATS event publisher connected")

        # Start OTel Exporter
        try:
            from memu.otel_exporter import OTelExporterTask
            import asyncio
            otel_task = OTelExporterTask(_nats_cluster)
            asyncio.create_task(otel_task.start())
            logger.info("OTel Exporter started")
        except Exception as e:
            logger.error(f"Failed to start OTel Exporter: {e}")

    except Exception as e:
        logger.warning("NATS connection failed (API will work without events): %s", e)
        _nats_cluster = None
        _nats_publisher = None

    yield
    if _nats_cluster:
        await _nats_cluster.close()
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

# ---------------------------------------------------------------------------
# Tenant resolution + RLS enforcement
# Uses tenancy.py module for core logic. API key â†’ tenant_id â†’ RLS context.
# ---------------------------------------------------------------------------


class AuthContext(str):
    """Authenticated request context with tenant information.
    
    Extends str for backward compatibility â€” existing endpoints that
    type-hint `_key: str` will still work. Access tenant_id via .tenant_id.
    """
    tenant_id: UUID

    def __new__(cls, api_key: str, tenant_id: UUID | None = None):
        instance = super().__new__(cls, api_key)
        instance.tenant_id = tenant_id or DEFAULT_TENANT_ID
        return instance


async def verify_api_key(key: str | None = Security(api_key_header)) -> AuthContext:
    """Verify API key and resolve tenant context for RLS."""
    if not key or key != MEMU_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    tid = await resolve_tenant_id(pool, key) if pool else DEFAULT_TENANT_ID
    return AuthContext(api_key=key, tenant_id=tid)


@asynccontextmanager
async def _tenant_conn(auth: AuthContext):
    """Helper: acquire a tenant-scoped connection (sets RLS context).
    
    Usage in endpoints:
        auth = Depends(verify_api_key)
        async with _tenant_conn(auth) as conn:
            rows = await conn.fetch("SELECT * FROM memories")  # RLS filters
    """
    async with tenant_connection(pool, auth.tenant_id) as conn:
        yield conn

# TODO: MCP_MOUNT â€” Rosie to mount MCP server here
# TODO: OTEL_STARTUP â€” Macklemore to wire OTel exporter into lifespan
# TODO: MCP_MOUNT — Rosie to mount MCP server here


def _coerce_memory_type(raw: str | None) -> MemoryType | None:
    if raw is None:
        return None
    try:
        return MemoryType(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid memory_type: {raw}")



# --- Embedding ---

async def get_embedding(text: str) -> list[float] | None:
    """Get embedding vector from any OpenAI-compatible API (Ollama, OpenAI, etc.).
    Falls back to FastEmbed (local) if API fails, then None if both fail."""

    # 1. Try OpenAI-compatible /v1 or Ollama /api endpoint
    if EMBEDDING_BASE_URL and (OPENAI_API_KEY or "ollama" in EMBEDDING_BASE_URL) and "BAAI" not in EMBEDDING_MODEL:
        headers = {"Content-Type": "application/json"}
        if OPENAI_API_KEY:
            headers["Authorization"] = f"Bearer {OPENAI_API_KEY}"

        async def _try_embedding(url: str, body: dict) -> list[float] | None:
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    r = await client.post(url, headers=headers, json=body)
                    r.raise_for_status()
                    res = r.json()
                    if "data" in res and res["data"]:
                        emb = res["data"][0].get("embedding")
                        if emb is not None:
                            return emb
                    if "embedding" in res:
                        return res["embedding"]
            except Exception as e:
                logger.warning("Embedding probe failed for %s: %s", url, e)
            return None

        # Modern Ollama/OpenAI-compatible endpoint
        emb = await _try_embedding(
            f"{EMBEDDING_BASE_URL.rstrip('/')}/v1/embeddings",
            {"input": text, "model": EMBEDDING_MODEL, "dimensions": EMBEDDING_DIMS},
        )
        if emb is not None and len(emb) == EMBEDDING_DIMS:
            return emb

        # Legacy Ollama endpoint (some self-hosted builds still use this shape)
        emb = await _try_embedding(
            f"{EMBEDDING_BASE_URL.rstrip('/')}/api/embeddings",
            {"model": EMBEDDING_MODEL, "prompt": text},
        )
        if emb is not None and len(emb) == EMBEDDING_DIMS:
            return emb
        if emb is not None:
            logger.warning("Primary embedding dim mismatch: got %d, expected %d", len(emb), EMBEDDING_DIMS)

        logger.warning("Primary embedding API failed, trying FastEmbed fallback")

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


def _normalize_custom_id(custom_id: str | None) -> str | None:
    if not custom_id:
        return None
    normalized = custom_id.strip()
    return normalized or None


def _merge_tags(existing: list[str] | None, new_tags: list[str] | None) -> list[str]:
    merged = {tag.strip() for tag in (existing or []) + (new_tags or []) if isinstance(tag, str) and tag.strip()}
    return sorted(merged)


def _secret_hygiene_hits(content: str, metadata: dict[str, Any] | None = None) -> list[str]:
    if not SECRET_HYGIENE_ENABLED:
        return []

    haystacks = [content]
    if metadata:
        try:
            haystacks.append(json.dumps(metadata, sort_keys=True))
        except Exception:
            haystacks.append(str(metadata))
    joined = "\n".join(haystacks)
    lowered = joined.lower()

    hits: list[str] = []
    if "authorization: bearer " in lowered or "x-api-key" in lowered:
        hits.append("authorization-token")
    if any(token in lowered for token in ["api_key=", "api-key", "secret_key", "client_secret", "refresh_token", "password="]):
        hits.append("credential-assignment")
    if "-----begin" in lowered and "private key-----" in lowered:
        hits.append("private-key-material")
    if "sk-" in joined or "ghp_" in joined or "xoxb-" in joined:
        hits.append("live-token-pattern")
    return sorted(set(hits))


def _history_hygiene_hits(content: str) -> list[str]:
    if not HISTORY_HYGIENE_ENABLED:
        return []

    lowered = content.lower()
    hits: list[str] = []
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if any(line.startswith(": ") and ";" in line for line in lines):
        hits.append("bash-history-format")
    if len(lines) >= 3 and sum(1 for line in lines if any(line.startswith(prefix) for prefix in ("cd ", "ls", "pwd", "cat ", "git ", "docker ", "kubectl ", "openclaw "))) >= 3:
        hits.append("shell-history-noise")
    if any(marker in lowered for marker in [".zsh_history", ".bash_history", "history | tail", "fc -ln"]):
        hits.append("history-artifact")
    return sorted(set(hits))


def _enforce_memory_hygiene(req: MemoryCreate) -> None:
    if req.allow_hygiene_bypass:
        return
    secret_hits = _secret_hygiene_hits(req.content, req.metadata)
    history_hits = _history_hygiene_hits(req.content)
    if not secret_hits and not history_hits:
        return

    detail: dict[str, Any] = {"message": "memory write blocked by hygiene guardrails"}
    if secret_hits:
        detail["secret_hits"] = secret_hits
    if history_hits:
        detail["history_hits"] = history_hits
    raise HTTPException(status_code=422, detail=detail)


async def _find_duplicate_candidate(conn, *, embedding: list[float] | None, c_hash: str, custom_id: str | None):
    if custom_id:
        existing = await conn.fetchrow(
            "SELECT id, 1.0::float AS similarity FROM memories WHERE custom_id = $1 LIMIT 1",
            custom_id,
        )
        if existing:
            return existing

    if embedding is not None:
        vec = f"vector({EMBEDDING_DIMS})"
        return await conn.fetchrow(
            f"""
            SELECT id, 1 - (embedding <=> $1::{vec}) AS similarity
            FROM memories
            WHERE content_hash = $2 OR (1 - (embedding <=> $1::{vec})) > $3
            ORDER BY similarity DESC
            LIMIT 1
            """,
            str(embedding),
            c_hash,
            DEDUP_THRESHOLD,
        )

    return await conn.fetchrow(
        "SELECT id, 1.0::float AS similarity FROM memories WHERE content_hash = $1 LIMIT 1",
        c_hash,
    )


async def _upsert_memory(conn, req: MemoryCreate, *, embedding: list[float] | None, tenant_id: UUID):
    c_hash = content_hash(req.content)
    custom_id = _normalize_custom_id(req.custom_id)
    existing = await _find_duplicate_candidate(conn, embedding=embedding, c_hash=c_hash, custom_id=custom_id)
    merged_tags = _merge_tags([], req.tags)

    if existing and should_deduplicate(existing["similarity"], DEDUP_THRESHOLD):
        return await conn.fetchrow(
            """
            UPDATE memories SET
                access_count = access_count + 1,
                duplicate_count = COALESCE(duplicate_count, 0) + 1,
                confidence = GREATEST(confidence, $2),
                metadata = metadata || $3::jsonb,
                tags = ARRAY(
                    SELECT DISTINCT tag
                    FROM unnest(COALESCE(tags, '{}'::text[]) || $4::text[]) AS tag
                    WHERE tag IS NOT NULL AND tag <> ''
                    ORDER BY tag
                ),
                custom_id = COALESCE(custom_id, $5),
                updated_at = NOW()
            WHERE id = $1
            RETURNING *
            """,
            existing["id"],
            req.confidence,
            json.dumps(req.metadata) if req.metadata else "{}",
            merged_tags,
            custom_id,
        )

    return await conn.fetchrow(
        """
        INSERT INTO memories (
            content, embedding, memory_type, agent_id, metadata, parent_id,
            confidence, content_hash, tenant_id, tags, custom_id
        )
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11)
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
        tenant_id,
        merged_tags,
        custom_id,
    )


async def _table_max_timestamp(conn, table_name: str) -> datetime | None:
    try:
        return await conn.fetchval(f"SELECT MAX(updated_at) FROM {table_name}")
    except Exception:
        return None


def _freshness_payload(ts: datetime | None, *, now: datetime, stale_after_minutes: int) -> dict[str, Any]:
    if ts is None:
        return {"updated_at": None, "age_seconds": None, "stale": True}
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now - ts).total_seconds())
    return {
        "updated_at": ts.isoformat(),
        "age_seconds": round(age_seconds, 3),
        "stale": age_seconds > stale_after_minutes * 60,
    }


# --- Helpers ---

def _row_to_memory(row) -> Memory:
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    # Handle tags column gracefully (may not exist in older schemas)
    tags = row.get("tags") or []

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
        tags=tags,
        custom_id=row.get("custom_id"),
        duplicate_count=row.get("duplicate_count") or 0,
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
    _enforce_memory_hygiene(req)
    embedding = await get_embedding(req.content)

    async with _tenant_conn(_key) as conn:
        tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)
        row = await _upsert_memory(conn, req, embedding=embedding, tenant_id=tenant_id)

    memory = _row_to_memory(row)

    # Publish NATS event for every memory write
    if _nats_publisher:
        try:
            await _nats_publisher.publish_memory_written(
                agent_id=req.agent_id or "unknown",
                memory_id=str(memory.id),
                content=req.content,
                memory_type=req.memory_type.value,
                metadata=req.metadata,
            )
        except Exception as e:
            logger.warning("NATS publish failed for memory write: %s", e)

    return memory


@app.get("/memories/search")
async def memories_search_compat(
    q: str,
    agent: str | None = None,
    agent_id: str | None = None,
    memory_type: str | None = None,
    min_confidence: float = 0.0,
    limit: int = 10,
    temporal_weight: float = DECAY_RATE,
    _key: str = Depends(verify_api_key),
):
    """Backward-compatible GET alias for memory search."""
    req = SearchRequest(
        query=q,
        limit=limit,
        agent_id=agent_id or agent,
        memory_type=_coerce_memory_type(memory_type),
        min_confidence=min_confidence,
        temporal_weight=temporal_weight,
    )
    return await search_memories(req, _key=_key)


@app.get("/memories/{memory_id}", response_model=Memory)
async def get_memory(memory_id: UUID, _key: str = Depends(verify_api_key)):
    async with _tenant_conn(_key) as conn:
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
    async with _tenant_conn(_key) as conn:
        result = await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True}


@app.post("/search", response_model=list[SearchResult])
async def search_memories(req: SearchRequest, _key: str = Depends(verify_api_key)):
    strategy = (req.search_strategy or "hybrid").lower()
    embedding = await get_embedding(req.query) if strategy != "text" else None

    filters = []
    vector_filters = []
    text_filters = []
    if req.agent_id:
        filters.append(("agent_id = ${idx}", req.agent_id))
    if req.memory_type:
        filters.append(("memory_type = ${idx}", req.memory_type.value))
    if req.min_confidence > 0:
        filters.append(("confidence >= ${idx}", req.min_confidence))
    if req.tags:
        filters.append(("tags @> ${idx}", req.tags))

    async with _tenant_conn(_key) as conn:
        vector_params: list[Any] = []
        text_params: list[Any] = [req.query]
        if embedding is not None:
            vector_params.append(str(embedding))

        vec_idx = 2
        text_idx = 2
        for template, value in filters:
            vector_filters.append(template.replace("{idx}", str(vec_idx)))
            vector_params.append(value)
            vec_idx += 1

            text_filters.append(template.replace("{idx}", str(text_idx + 1)))
            text_params.append(value)
            text_idx += 1

        vector_where = (" AND " + " AND ".join(vector_filters)) if vector_filters else ""
        text_where = (" AND " + " AND ".join(text_filters)) if text_filters else ""

        vector_rows = []
        if embedding is not None and strategy in {"hybrid", "vector"}:
            vec = f"vector({EMBEDDING_DIMS})"
            await conn.execute("SET LOCAL hnsw.ef_search = 100")
            vector_rows = await conn.fetch(
                f"""
                SELECT *, 1 - (embedding <=> $1::{vec}) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL{vector_where}
                ORDER BY embedding <=> $1::{vec}
                LIMIT {max(req.limit * 4, 20)}
                """,
                *vector_params,
            )

        text_rows = []
        if strategy in {"hybrid", "text"} or embedding is None:
            text_rows = await conn.fetch(
                f"""
                SELECT *,
                       ts_rank_cd(to_tsvector('english', coalesce(content, '')), plainto_tsquery('english', $1)) AS text_score
                FROM memories
                WHERE to_tsvector('english', coalesce(content, '')) @@ plainto_tsquery('english', $1){text_where}
                ORDER BY text_score DESC, updated_at DESC
                LIMIT {max(req.limit * 4, 20)}
                """,
                *text_params,
            )
            if not text_rows:
                fallback_filters = ["content ILIKE $1"]
                fallback_params: list[Any] = [f"%{req.query}%"]
                idx = 2
                for _template, value in filters:
                    fallback_filters.append(_template.replace("{idx}", str(idx)))
                    fallback_params.append(value)
                    idx += 1
                where = " AND ".join(fallback_filters)
                text_rows = await conn.fetch(
                    f"""
                    SELECT *, 0.15::float8 AS text_score
                    FROM memories
                    WHERE {where}
                    ORDER BY updated_at DESC
                    LIMIT {max(req.limit * 4, 20)}
                    """,
                    *fallback_params,
                )

        if embedding is None and strategy == "vector":
            logger.warning("Embedding service unavailable for /search with vector strategy; using text fallback")
            strategy = "text"

        if strategy == "vector":
            fused_map = {str(row["id"]): 1.0 for row in vector_rows}
        elif strategy == "text":
            fused_map = {str(row["id"]): 1.0 for row in text_rows}
        else:
            fused_map = reciprocal_rank_fusion(
                [
                    normalize_ranked_rows(vector_rows, "similarity"),
                    normalize_ranked_rows(text_rows, "text_score"),
                ]
            )

        combined_rows = {str(r["id"]): r for r in vector_rows}
        for row in text_rows:
            combined_rows.setdefault(str(row["id"]), row)

        candidate_ids = list(fused_map.keys())[: max(req.limit * 4, 20)]
        graph_rows = []
        if req.graph_depth > 0 and candidate_ids:
            graph_rows = await conn.fetch(
                """
                SELECT
                    CASE
                        WHEN ml.source_id::text = ANY($1::text[]) THEN ml.source_id::text
                        ELSE ml.target_id::text
                    END AS memory_id,
                    neighbor.created_at,
                    ml.strength
                FROM memory_links ml
                JOIN memories neighbor
                  ON neighbor.id = CASE
                      WHEN ml.source_id::text = ANY($1::text[]) THEN ml.target_id
                      ELSE ml.source_id
                  END
                WHERE ml.source_id::text = ANY($1::text[]) OR ml.target_id::text = ANY($1::text[])
                ORDER BY ml.strength DESC, neighbor.created_at DESC
                LIMIT 200
                """,
                candidate_ids,
            )

        graph_by_memory: dict[str, list[Any]] = {}
        for row in graph_rows:
            graph_by_memory.setdefault(str(row["memory_id"]), []).append(row)

        ranked: list[SearchResult] = []
        for memory_id, fused_score in fused_map.items():
            row = combined_rows.get(memory_id)
            if row is None:
                continue
            similarity = float(row.get("similarity") or row.get("text_score") or 0.0)
            temporal_score = compute_final_score(
                similarity=similarity if similarity > 0 else 0.35,
                created_at=row["created_at"],
                access_count=row["access_count"],
                decay_rate=DECAY_RATE,
                temporal_weight=req.temporal_weight,
            )
            graph_boost = compute_graph_temporal_boost(memory_id, graph_by_memory.get(memory_id, []))
            ranked.append(
                SearchResult(
                    memory=_row_to_memory(row),
                    similarity=similarity,
                    final_score=blend_hybrid_score(fused_score, temporal_score, graph_boost),
                )
            )

        ranked.sort(key=lambda r: r.final_score, reverse=True)
        final = ranked[: req.limit]

        if final:
            ids = [r.memory.id for r in final]
            await conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ANY($1)",
                ids,
            )

    try:
        emb_str = str(embedding) if embedding else None
        async with _tenant_conn(_key) as conn:
            await conn.execute(
                """
                INSERT INTO search_history (query, agent_id, results_count, search_type, metadata, embedding)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                """,
                req.query,
                req.agent_id or "system",
                len(final),
                strategy,
                json.dumps({
                    "temporal_weight": req.temporal_weight,
                    "limit": req.limit,
                    "graph_depth": req.graph_depth,
                    "hybrid": strategy == "hybrid",
                }),
                emb_str,
            )
    except Exception as e:
        logger.debug(f"Failed to log search history: {e}")

    if _nats_publisher:
        try:
            await _nats_publisher.publish_search_logged(
                agent_id=req.agent_id or "unknown",
                query=req.query,
                source=f"memu_search_{strategy}",
                result_count=len(final),
            )
        except Exception as e:
            logger.warning("NATS publish failed for search: %s", e)

    return final


@app.get("/api/v1/memu/search")
async def memu_search_compat(
    q: str,
    agent: str | None = None,
    agent_id: str | None = None,
    limit: int = 10,
    memory_type: str | None = None,
    search_strategy: str = "hybrid",
    graph_depth: int = 1,
    _key: str = Depends(verify_api_key),
):
    req = SearchRequest(
        query=q,
        limit=limit,
        agent_id=agent_id or agent,
        memory_type=_coerce_memory_type(memory_type),
        search_strategy=search_strategy,
        graph_depth=graph_depth,
    )
    return await search_memories(req, _key=_key)


@app.post("/search/hybrid", response_model=list[SearchResult])
async def hybrid_search(req: SearchRequest, _key: str = Depends(verify_api_key)):
    req.search_strategy = "hybrid"
    return await search_memories(req, _key=_key)


@app.get("/api/v1/memu/retrieval/status")
async def retrieval_status(_key: str = Depends(verify_api_key)):
    async with _tenant_conn(_key) as conn:
        indexes = await conn.fetch(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('memories', 'memory_links')
              AND (
                    indexdef ILIKE '%USING hnsw%'
                 OR indexdef ILIKE '%to_tsvector%'
                 OR indexname ILIKE '%memory_links%'
              )
            ORDER BY tablename, indexname
            """
        )
    return {
        "ok": True,
        "hybrid_default": True,
        "hnsw_active": any("USING hnsw" in row["indexdef"] for row in indexes),
        "indexes": [dict(row) for row in indexes],
    }


@app.get("/search-text")
async def search_text_compat(
    q: str,
    agent_id: str | None = None,
    memory_type: str | None = None,
    limit: int = 10,
    _key: str = Depends(verify_api_key),
):
    """Backward-compatible query-param form for search-text."""
    return await search_text(q, agent_id=agent_id, memory_type=memory_type, limit=limit, _key=_key)


@app.post("/search-text")
async def search_text(
    query: str,
    agent_id: str | None = None,
    memory_type: str | None = None,
    limit: int = 10,
    _key: str = Depends(verify_api_key),
):
    """
    Full-text search using PostgreSQL ILIKE â€” no embeddings needed.
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

    async with _tenant_conn(_key) as conn:
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
    """RAG chat â€” retrieves relevant memories and generates an answer."""
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
    custom_ids = req.custom_ids or []
    if custom_ids and len(custom_ids) != len(chunks):
        raise HTTPException(status_code=422, detail="custom_ids length must match chunk count")

    imported = 0
    dupes = 0
    deduplicated_updated = 0
    tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)

    async with _tenant_conn(_key) as conn:
        for idx, chunk in enumerate(chunks):
            try:
                item_req = MemoryCreate(
                    content=chunk,
                    memory_type=req.memory_type,
                    agent_id=req.agent_id,
                    custom_id=custom_ids[idx] if custom_ids else None,
                    allow_hygiene_bypass=req.allow_hygiene_bypass,
                )
                _enforce_memory_hygiene(item_req)
                embedding = await get_embedding(chunk)
                existing = await _find_duplicate_candidate(
                    conn,
                    embedding=embedding,
                    c_hash=content_hash(chunk),
                    custom_id=_normalize_custom_id(item_req.custom_id),
                )
                row = await _upsert_memory(conn, item_req, embedding=embedding, tenant_id=tenant_id)
                if existing and should_deduplicate(existing["similarity"], DEDUP_THRESHOLD):
                    deduplicated_updated += 1
                    dupes += 1
                else:
                    imported += 1
            except HTTPException:
                raise
            except Exception:
                continue

    return BulkImportResponse(
        imported=imported,
        duplicates_skipped=dupes,
        deduplicated_updated=deduplicated_updated,
    )


@app.post("/memories/dedupe")
async def dedupe_memories(
    dry_run: bool = False,
    limit: int = 100,
    _key: str = Depends(verify_api_key),
):
    """Collapse duplicate memories by custom_id first, then content_hash."""
    merged_groups = 0
    removed_ids: list[str] = []

    async with _tenant_conn(_key) as conn:
        custom_groups = await conn.fetch(
            """
            SELECT custom_id AS dedupe_key, array_agg(id ORDER BY updated_at DESC, created_at DESC) AS ids
            FROM memories
            WHERE custom_id IS NOT NULL
            GROUP BY custom_id
            HAVING count(*) > 1
            LIMIT $1
            """,
            limit,
        )
        for group in custom_groups:
            ids = list(group["ids"])
            keeper, duplicates = ids[0], ids[1:]
            if not duplicates:
                continue
            merged_groups += 1
            removed_ids.extend(str(item) for item in duplicates)
            if not dry_run:
                await conn.execute(
                    """
                    UPDATE memories
                    SET duplicate_count = COALESCE(duplicate_count, 0) + $2,
                        access_count = access_count + $2,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    keeper,
                    len(duplicates),
                )
                await conn.execute("DELETE FROM memories WHERE id = ANY($1::uuid[])", duplicates)

        hash_groups = await conn.fetch(
            """
            SELECT content_hash AS dedupe_key, array_agg(id ORDER BY updated_at DESC, created_at DESC) AS ids
            FROM memories
            WHERE content_hash IS NOT NULL
            GROUP BY content_hash
            HAVING count(*) > 1
            LIMIT $1
            """,
            limit,
        )
        for group in hash_groups:
            ids = [item for item in group["ids"] if str(item) not in removed_ids]
            if len(ids) < 2:
                continue
            keeper, duplicates = ids[0], ids[1:]
            merged_groups += 1
            removed_ids.extend(str(item) for item in duplicates)
            if not dry_run:
                await conn.execute(
                    """
                    UPDATE memories
                    SET duplicate_count = COALESCE(duplicate_count, 0) + $2,
                        access_count = access_count + $2,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    keeper,
                    len(duplicates),
                )
                await conn.execute("DELETE FROM memories WHERE id = ANY($1::uuid[])", duplicates)

    return {
        "ok": True,
        "dry_run": dry_run,
        "merged_groups": merged_groups,
        "duplicates_removed": len(removed_ids),
        "removed_ids": removed_ids,
    }


@app.get("/api/v1/memu/status/freshness")
async def status_freshness(_key: str = Depends(verify_api_key)):
    now = datetime.now(timezone.utc)
    async with _tenant_conn(_key) as conn:
        memories_ts = await _table_max_timestamp(conn, "memories")
        backlog_ts = await _table_max_timestamp(conn, "backlog")
        blocks_ts = await _table_max_timestamp(conn, "memory_blocks")

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "stale_after_minutes": STATUS_FRESHNESS_MAX_AGE_MINUTES,
        "streams": {
            "memories": _freshness_payload(memories_ts, now=now, stale_after_minutes=STATUS_FRESHNESS_MAX_AGE_MINUTES),
            "tasks": _freshness_payload(backlog_ts, now=now, stale_after_minutes=STATUS_FRESHNESS_MAX_AGE_MINUTES),
            "memory_blocks": _freshness_payload(blocks_ts, now=now, stale_after_minutes=STATUS_FRESHNESS_MAX_AGE_MINUTES),
        },
    }


@app.post("/tasks", response_model=Task)
async def create_task(req: TaskCreate, _key: str = Depends(verify_api_key)):
    tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO backlog (task, priority, owner_id, lane, metadata, dependency_id, tenant_id)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            RETURNING *
            """,
            req.task,
            req.priority.value,
            req.owner_id,
            req.lane,
            json.dumps(req.metadata) if req.metadata else "{}",
            req.dependency_id,
            tenant_id,
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
    async with _tenant_conn(_key) as conn:
        rows = await conn.fetch(f"SELECT * FROM backlog{where} ORDER BY priority ASC, created_at DESC", *params)
    return [_row_to_task(row) for row in rows]


@app.patch("/tasks/{task_id}", response_model=Task)
async def update_task(task_id: UUID, req: TaskUpdate, _key: str = Depends(verify_api_key)):
    handoff_target = (req.metadata or {}).get("handoff_to") if req.metadata else None
    if (req.status in {TaskStatus.done, TaskStatus.blocked} or handoff_target) and not (req.evidence and req.evidence.strip()):
        raise HTTPException(
            status_code=422,
            detail="evidence is required when marking a task done/blocked or creating a handoff",
        )

    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            """
            UPDATE backlog
            SET
                status = COALESCE($2, status),
                evidence = COALESCE($3, evidence),
                metadata = COALESCE(metadata, '{}'::jsonb) || $4::jsonb,
                updated_at = NOW()
            WHERE id = $1
            RETURNING *
            """,
            task_id,
            req.status.value if req.status else None,
            req.evidence.strip() if req.evidence else None,
            json.dumps(req.metadata) if req.metadata else "{}",
        )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return _row_to_task(row)


# --- A-MEM Link Layer ---

class LinkCreate(BaseModel):
    source_id: UUID
    target_id: UUID
    relationship: str = "similar"
    strength: float = 0.5


@app.post("/api/v1/memu/links")
async def create_link(req: LinkCreate, _key: str = Depends(verify_api_key)):
    async with _tenant_conn(_key) as conn:
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
    async with _tenant_conn(_key) as conn:
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

    async with _tenant_conn(_key) as conn:
        vec = f"vector({EMBEDDING_DIMS})"
        rows = await conn.fetch(
            f"""
            SELECT *,
                1 - (embedding <=> $1::{vec}) AS similarity,
                EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0 AS age_days
            FROM memories
            WHERE embedding IS NOT NULL AND {where}
            ORDER BY embedding <=> $1::{vec}
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

    async with _tenant_conn(_key) as conn:
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



# --- Forensics Endpoints (Compliance Engine) ---


@app.get("/api/forensics/{task_id}")
async def forensics_task_bundle(task_id: str, _key: str = Depends(verify_api_key)):
    """Aggregate the DAG, events, and bi-temporal memory context for a task.
    
    This is the core forensic playback endpoint â€” given a task_id, reconstruct
    exactly what happened, what the agent knew, and what it decided.
    
    Returns a downloadable JSON "Incident Bundle" suitable for compliance
    officers, insurance adjusters, and legal review.
    """
    from datetime import datetime as dt

    bundle: dict[str, Any] = {
        "forensics_version": "1.0.0",
        "task_id": task_id,
        "generated_at": dt.now().isoformat(),
        "events": [],
        "task_state": None,
        "context_at_execution": [],
        "gateway_signatures": [],
        "dead_letter_queue": None,
    }

    async with _tenant_conn(_key) as conn:
        # 1. Get all events for this task (ordered chronologically)
        try:
            events = await conn.fetch(
                """
                SELECT event_id, timestamp, gateway_id, event_type, payload, 
                       parent_event, signature, compute_cost
                FROM events
                WHERE task_id = $1::uuid
                ORDER BY timestamp ASC
                """,
                task_id,
            )
            bundle["events"] = [
                {
                    "event_id": str(row["event_id"]),
                    "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
                    "gateway_id": row["gateway_id"],
                    "event_type": row["event_type"],
                    "payload": row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"]) if row["payload"] else {},
                    "signature": row["signature"],
                    "compute_cost": row["compute_cost"],
                    "signature_present": bool(row["signature"]),
                }
                for row in events
            ]
        except Exception as e:
            bundle["events_error"] = str(e)

        # 2. Get task state from tasks table
        try:
            task_row = await conn.fetchrow(
                """
                SELECT task_id, root_prompt_id, parent_task_id, title, description,
                       status, assigned_gateway, compute_budget, compute_spent, created_at, updated_at
                FROM tasks
                WHERE task_id = $1::uuid
                """,
                task_id,
            )
            if task_row:
                bundle["task_state"] = {
                    k: (str(v) if isinstance(v, UUID) else v.isoformat() if hasattr(v, 'isoformat') else v)
                    for k, v in dict(task_row).items()
                }

                # 2b. Get root prompt if available
                if task_row["root_prompt_id"]:
                    root = await conn.fetchrow(
                        "SELECT content, user_id, metadata, created_at FROM root_prompts WHERE id = $1",
                        task_row["root_prompt_id"],
                    )
                    if root:
                        bundle["root_prompt"] = {
                            "content": root["content"],
                            "user_id": root["user_id"],
                            "created_at": root["created_at"].isoformat() if root["created_at"] else None,
                        }
        except Exception as e:
            bundle["task_state_error"] = str(e)

        # 3. Get bi-temporal memory context at the time of first event
        if bundle["events"]:
            first_event_time = bundle["events"][0].get("timestamp")
            if first_event_time:
                try:
                    context_memories = await conn.fetch(
                        """
                        SELECT id, content, memory_type, agent_id, confidence, 
                               created_at, valid_from, valid_to
                        FROM memories
                        WHERE valid_from <= $1::timestamptz
                          AND (valid_to IS NULL OR valid_to > $1::timestamptz)
                        ORDER BY created_at DESC
                        LIMIT 50
                        """,
                        first_event_time,
                    )
                    bundle["context_at_execution"] = [
                        {
                            "memory_id": str(row["id"]),
                            "content": row["content"][:500],
                            "memory_type": row["memory_type"],
                            "agent_id": row["agent_id"],
                            "confidence": row["confidence"],
                            "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
                            "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
                        }
                        for row in context_memories
                    ]
                except Exception as e:
                    bundle["context_error"] = str(e)

        # 4. Collect unique gateway signatures
        seen_gateways = set()
        for evt in bundle["events"]:
            gw = evt.get("gateway_id")
            if gw and gw not in seen_gateways:
                seen_gateways.add(gw)
                # Look up gateway's public key from registry
                try:
                    gw_row = await conn.fetchrow(
                        "SELECT capabilities, status, metadata FROM gateway_registry WHERE gateway_id = $1",
                        gw,
                    )
                    bundle["gateway_signatures"].append({
                        "gateway_id": gw,
                        "public_key": gw_row["metadata"].get("public_key") if gw_row and gw_row["metadata"] else None,
                        "status": gw_row["status"] if gw_row else "unknown",
                        "events_signed": sum(1 for e in bundle["events"] if e["gateway_id"] == gw and e["signature_present"]),
                        "events_unsigned": sum(1 for e in bundle["events"] if e["gateway_id"] == gw and not e["signature_present"]),
                    })
                except Exception:
                    bundle["gateway_signatures"].append({
                        "gateway_id": gw,
                        "public_key": None,
                        "events_signed": 0,
                        "events_unsigned": 0,
                    })

        # 5. Check DLQ for this task
        try:
            dlq_row = await conn.fetchrow(
                """
                SELECT task_id, failure_count, failures, root_cause_diagnosis, 
                       proposed_amendment, entered_at, resolved_at
                FROM dead_letter_queue
                WHERE task_id = $1::uuid
                """,
                task_id,
            )
            if dlq_row:
                bundle["dead_letter_queue"] = {
                    k: (str(v) if isinstance(v, UUID) else v.isoformat() if hasattr(v, 'isoformat') else v)
                    for k, v in dict(dlq_row).items()
                }
        except Exception as e:
            bundle["dlq_error"] = str(e)

        # 6. Get checkpoints for this task
        try:
            checkpoints = await conn.fetch(
                """
                SELECT id, gateway_id, checkpoint_seq, progress_pct, tokens_consumed, created_at
                FROM checkpoints
                WHERE task_id = $1::uuid
                ORDER BY checkpoint_seq ASC
                """,
                task_id,
            )
            bundle["checkpoints"] = [
                {
                    "checkpoint_id": str(row["id"]),
                    "gateway_id": row["gateway_id"],
                    "sequence": row["checkpoint_seq"],
                    "progress_pct": row["progress_pct"],
                    "tokens_consumed": row["tokens_consumed"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                }
                for row in checkpoints
            ]
        except Exception as e:
            bundle["checkpoints_error"] = str(e)

        # 7. Get lane locks that were active during this task
        try:
            lane_locks = await conn.fetch(
                """
                SELECT lane_id, gateway_id, fencing_token, acquired_at, expires_at
                FROM lane_locks
                WHERE task_id = $1::uuid
                """,
                task_id,
            )
            bundle["lane_locks"] = [
                {
                    "lane_id": row["lane_id"],
                    "gateway_id": row["gateway_id"],
                    "fencing_token": row["fencing_token"],
                    "acquired_at": row["acquired_at"].isoformat() if row["acquired_at"] else None,
                    "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                }
                for row in lane_locks
            ]
        except Exception as e:
            bundle["lane_locks_error"] = str(e)

    # Summary stats
    bundle["summary"] = {
        "total_events": len(bundle.get("events", [])),
        "unique_gateways": len(bundle.get("gateway_signatures", [])),
        "context_memories": len(bundle.get("context_at_execution", [])),
        "checkpoints": len(bundle.get("checkpoints", [])),
        "lane_locks": len(bundle.get("lane_locks", [])),
        "has_dlq_entry": bundle.get("dead_letter_queue") is not None,
        "all_events_signed": all(e.get("signature_present") for e in bundle.get("events", [])),
    }

    return bundle


@app.get("/api/forensics/playback/{task_id}")
async def forensics_playback(task_id: str, _key: str = Depends(verify_api_key)):
    """Time Machine: reconstruct the agent's exact brain state at the moment 
    it made each decision on this task.
    
    Returns a timeline of decisions with their bi-temporal memory context,
    allowing perfect reconstruction of what the agent knew vs what it did.
    """
    from datetime import datetime as dt

    timeline: list[dict[str, Any]] = []

    async with _tenant_conn(_key) as conn:
        # Get all decision/execution events for this task
        # Note: events table may not exist in memU-only deployments
        try:
            events = await conn.fetch(
                """
                SELECT event_id, timestamp, gateway_id, event_type, payload, signature
                FROM events
                WHERE task_id = $1::uuid
                  AND event_type IN ('decision_made', 'task_completed', 'task_failed', 
                                     'rollback_executed', 'dlq_enqueued')
                ORDER BY timestamp ASC
                """,
                task_id,
            )
        except Exception as e:
            # events table doesn't exist in this environment
            return {
                "task_id": task_id,
                "timeline": [],
                "decision_count": 0,
                "generated_at": dt.now().isoformat(),
                "note": f"Events table not available in this environment: {e}",
            }

        for event in events:
            ts = event["timestamp"]
            # Query bi-temporal memory at this exact moment
            memories = await conn.fetch(
                """
                SELECT id, content, memory_type, agent_id, confidence, valid_from
                FROM memories
                WHERE valid_from <= $1
                  AND (valid_to IS NULL OR valid_to > $1)
                ORDER BY confidence DESC, created_at DESC
                LIMIT 20
                """,
                ts,
            )

            timeline.append({
                "event_id": str(event["event_id"]),
                "timestamp": ts.isoformat(),
                "event_type": event["event_type"],
                "gateway_id": event["gateway_id"],
                "payload": event["payload"],
                "signed": bool(event["signature"]),
                "agent_brain_state": {
                    "memories_in_scope": len(memories),
                    "memories": [
                        {
                            "id": str(m["id"]),
                            "content_preview": m["content"][:200],
                            "type": m["memory_type"],
                            "confidence": m["confidence"],
                        }
                        for m in memories
                    ],
                },
            })

    return {
        "task_id": task_id,
        "timeline": timeline,
        "decision_count": len(timeline),
        "generated_at": dt.now().isoformat(),
    }



# --- Lane Coordination (NATS bridge for external agents) ---

class LaneMessage(BaseModel):
    task_id: str
    owner: str
    lane: str
    fencing_token: str
    state: str  # claimed, in_progress, blocked, done

@app.post("/api/v1/lanes/publish")
async def publish_lane_message(msg: LaneMessage, api_key: str = Security(api_key_header)):
    """Publish a lane coordination message to NATS swarm.tasks.<lane>.* subject."""
    verify_api_key(api_key)
    if not _nats_publisher:
        raise HTTPException(503, "NATS not connected")

    subject = f"swarm.tasks.{msg.lane}.{msg.state}"
    payload = {
        "task_id": msg.task_id,
        "owner": msg.owner,
        "lane": msg.lane,
        "fencing_token": msg.fencing_token,
        "state": msg.state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        nc = _nats_publisher.cluster.active_connection
        await nc.publish(subject, json.dumps(payload).encode())
        await nc.flush()
        logger.info("Lane message published: %s -> %s", subject, msg.owner)
        return {"ok": True, "subject": subject, "payload": payload}
    except Exception as e:
        logger.error("Lane publish failed: %s", e)
        raise HTTPException(500, f"NATS publish failed: {e}")


@app.get("/api/v1/lanes/status")
async def get_lane_status(api_key: str = Security(api_key_header)):
    """Check NATS connectivity for lane coordination."""
    verify_api_key(api_key)
    connected = _nats_publisher is not None and _nats_publisher.cluster.active_connection is not None
    return {"ok": connected, "nats": "connected" if connected else "disconnected"}



# --- Search Vault / Recall Endpoints ---

@app.get("/api/v1/memu/search/recall")
async def recall_search_compat(
    q: str,
    limit: int = 5,
    agent: str | None = None,
    agent_id: str | None = None,
    _key: str = Depends(verify_api_key),
):
    return await recall_search(query=q, limit=limit, agent_id=agent_id or agent, _key=_key)


@app.get("/search/recall")
async def recall_search(
    query: str | None = None,
    q: str | None = None,
    limit: int = 5,
    agent_id: str | None = None,
    _key: str = Depends(verify_api_key)
):
    """
    Search the Vault (search_history) for similar past searches.
    Returns: List of {query, timestamp, agent_id, similarity}
    Supports both `query` (canonical) and `q` (backward-compatible).
    """
    normalized_query = query or q
    if not normalized_query:
        raise HTTPException(status_code=400, detail="query parameter is required")
    try:
        embedding = await get_embedding(normalized_query)
    except Exception as e:
        logger.error(f"Failed to embed query: {e}")
        raise HTTPException(status_code=500, detail="Embedding failure")

    if pool is None:
        raise HTTPException(status_code=503, detail="Database not initialized")

    async with _tenant_conn(_key) as conn:
        vec = f"vector({EMBEDDING_DIMS})"
        rows = await conn.fetch(
            f"""
            SELECT query, agent_id, created_at, results_count, 
                   1 - (embedding <=> $1::{vec}) as similarity
            FROM search_history
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::{vec}
            LIMIT $2
            """,
            str(embedding),
            limit,
        )
        
    return [dict(r) for r in rows]


# --- Tenant Management Endpoints ---

class TenantCreate(BaseModel):
    name: str
    slug: str
    plan: str = "free"
    metadata: dict[str, Any] = {}


class TenantResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    plan: str
    created_at: Any


@app.post("/api/v1/tenants", response_model=TenantResponse)
async def create_tenant(req: TenantCreate, _key: str = Depends(verify_api_key)):
    """Create a new tenant for multi-tenancy isolation."""
    async with _tenant_conn(_key) as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tenants (name, slug, plan, metadata)
                VALUES ($1, $2, $3, $4::jsonb)
                RETURNING id, name, slug, plan, created_at
                """,
                req.name,
                req.slug,
                req.plan,
                json.dumps(req.metadata),
            )
            return TenantResponse(**dict(row))
        except Exception as e:
            if "unique" in str(e).lower():
                raise HTTPException(status_code=409, detail=f"Tenant slug '{req.slug}' already exists")
            raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/tenants")
async def list_tenants(_key: str = Depends(verify_api_key)):
    """List all tenants."""
    async with _tenant_conn(_key) as conn:
        try:
            rows = await conn.fetch("SELECT id, name, slug, plan, created_at FROM tenants ORDER BY created_at")
            return [dict(r) for r in rows]
        except Exception as e:
            # tenants table may not exist yet
            return {"error": str(e), "note": "Run migration 009 to enable multi-tenancy"}


@app.get("/api/v1/tenants/{tenant_slug}")
async def get_tenant(tenant_slug: str, _key: str = Depends(verify_api_key)):
    """Get tenant details by slug."""
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, slug, plan, metadata, created_at FROM tenants WHERE slug = $1",
            tenant_slug,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant not found")
        return dict(row)

# --- Context Blocks (Stable Agent State) ---


@app.get("/api/v1/memu/blocks/{key:path}", response_model=MemoryBlock)
async def get_block(key: str, _key: str = Depends(verify_api_key)):
    """Get a context block by key (e.g. 'project:jiraflow:status')."""
    tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM memory_blocks WHERE key = $1 AND tenant_id = $2",
            key, tenant_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"Block '{key}' not found")
    return MemoryBlock(
        key=row["key"],
        content=row["content"],
        agent_owner=row["agent_owner"],
        metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@app.put("/api/v1/memu/blocks/{key:path}", response_model=MemoryBlock)
async def upsert_block(key: str, req: MemoryBlockCreate, _key: str = Depends(verify_api_key)):
    """Create or update a context block. Version auto-increments on update."""
    tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO memory_blocks (key, content, agent_owner, metadata, tenant_id, version)
            VALUES ($1, $2, $3, $4::jsonb, $5, 1)
            ON CONFLICT (key, tenant_id) DO UPDATE SET
                content = EXCLUDED.content,
                agent_owner = COALESCE(EXCLUDED.agent_owner, memory_blocks.agent_owner),
                metadata = memory_blocks.metadata || EXCLUDED.metadata,
                version = memory_blocks.version + 1,
                updated_at = NOW()
            RETURNING *
            """,
            key,
            req.content,
            req.agent_owner,
            json.dumps(req.metadata) if req.metadata else "{}",
            tenant_id,
        )

    block = MemoryBlock(
        key=row["key"],
        content=row["content"],
        agent_owner=row["agent_owner"],
        metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

    # Publish NATS event for block updates
    if _nats_publisher:
        try:
            nc = _nats_publisher.cluster.active_connection
            payload = {
                "key": key,
                "agent_owner": req.agent_owner,
                "version": block.version,
                "updated_at": block.updated_at.isoformat(),
            }
            await nc.publish(f"agent.block.updated.{key.replace(':', '.')}", json.dumps(payload).encode())
        except Exception as e:
            logger.warning("NATS publish failed for block update: %s", e)

    return block


@app.delete("/api/v1/memu/blocks/{key:path}")
async def delete_block(key: str, _key: str = Depends(verify_api_key)):
    """Delete a context block."""
    tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)
    async with _tenant_conn(_key) as conn:
        result = await conn.execute(
            "DELETE FROM memory_blocks WHERE key = $1 AND tenant_id = $2",
            key, tenant_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"Block '{key}' not found")
    return {"deleted": True, "key": key}


@app.get("/api/v1/memu/blocks")
async def list_blocks(
    agent: str | None = None,
    prefix: str | None = None,
    _key: str = Depends(verify_api_key),
):
    """List all context blocks, optionally filtered by agent or key prefix."""
    tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)
    filters = ["tenant_id = $1"]
    params: list[Any] = [tenant_id]
    idx = 2

    if agent:
        filters.append(f"agent_owner = ${idx}")
        params.append(agent)
        idx += 1
    if prefix:
        filters.append(f"key LIKE ${idx}")
        params.append(f"{prefix}%")
        idx += 1

    where = " AND ".join(filters)
    async with _tenant_conn(_key) as conn:
        rows = await conn.fetch(
            f"SELECT key, agent_owner, version, updated_at FROM memory_blocks WHERE {where} ORDER BY key",
            *params,
        )
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
