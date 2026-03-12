"""memU API â€” FastAPI application."""

from __future__ import annotations
from datetime import datetime, timezone, timedelta

import hashlib
import json
from memu.web_search_ingest import ingest_web_search
import os
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID
import re

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from memu.decay import compute_final_score, should_deduplicate

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
    GatewayLease,
    GatewayLeaseAcquireRequest,
    GatewayLeaseAcquireResponse,
    GatewayLeaseReleaseRequest,
    GatewayLeaseRenewRequest,
    Memory,
    MemoryCreate,
    MemoryType,
    SearchRequest,
    SearchResult,
    Task,
    TaskCreate,
    TaskStatus,
    TaskUpdate,
    TaskReviewRequest,
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
HNSW_ITERATIVE_SCAN = os.environ.get("HNSW_ITERATIVE_SCAN", "strict_order")
HNSW_MAX_SCAN_TUPLES = int(os.environ.get("HNSW_MAX_SCAN_TUPLES", "20000"))
IVFFLAT_MAX_PROBES = int(os.environ.get("IVFFLAT_MAX_PROBES", "10"))

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

memu_key_header = APIKeyHeader(name="X-MemU-Key", auto_error=False)
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


async def verify_api_key(
    memu_key: str | None = Security(memu_key_header),
    legacy_key: str | None = Security(api_key_header),
) -> AuthContext:
    """Verify API key and resolve tenant context for RLS.

    Prefer X-MemU-Key, but accept legacy X-API-Key for backward compatibility.
    """
    key = memu_key or legacy_key
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


def _validate_graph_identifier(graph_name: str) -> str:
    """Whitelist graph identifiers to prevent SQL injection in cypher wrapper SQL."""
    normalized = graph_name.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="graph_name is required")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", normalized):
        raise HTTPException(status_code=422, detail="invalid graph_name")
    return normalized


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
        risk_score=row.get("risk_score", 25) if hasattr(row, "get") else 25,
        source=row.get("source"),
        source_ref=row.get("source_ref"),
        project=row.get("project"),
        completion_criteria=row.get("completion_criteria"),
        review_status=row.get("review_status", "pending_review"),
        reviewer_id=row.get("reviewer_id"),
        reviewed_at=row.get("reviewed_at"),
        review_notes=row.get("review_notes"),
        retry_count=row.get("retry_count", 0),
        refine_status=row.get("refine_status", "pending"),
        refined_at=row.get("refined_at"),
        source_fingerprint=row.get("source_fingerprint"),
        menu_bucket=row.get("menu_bucket"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_gateway_lease(row) -> GatewayLease:
    task_state = row["task_state"]
    metadata = row["metadata"]
    if isinstance(task_state, str):
        task_state = json.loads(task_state)
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return GatewayLease(
        lease_key=row["lease_key"],
        owner_gateway=row["owner_gateway"],
        backup_gateway=row["backup_gateway"],
        lease_expires_at=row["lease_expires_at"],
        last_message_id=row["last_message_id"],
        last_reply_id=row["last_reply_id"],
        context_digest=row["context_digest"],
        task_state=task_state or {},
        metadata=metadata or {},
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _publish_gateway_lease_event(action: str, lease: GatewayLease):
    if not _nats_publisher:
        return
    try:
        subject = f"swarm.gateway.lease.{action}"
        payload = {
            "action": action,
            "lease_key": lease.lease_key,
            "owner_gateway": lease.owner_gateway,
            "backup_gateway": lease.backup_gateway,
            "lease_expires_at": lease.lease_expires_at.isoformat(),
            "version": lease.version,
            "last_message_id": lease.last_message_id,
            "last_reply_id": lease.last_reply_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        nc = _nats_publisher.cluster.active_connection
        await nc.publish(subject, json.dumps(payload).encode())
        await nc.flush()
    except Exception as e:
        logger.warning("NATS publish failed for gateway lease %s: %s", action, e)


def _expansion_schedule(base_limit: int, max_steps: int) -> list[int]:
    cap = min(400, max(base_limit, 1) * 8)
    limits: list[int] = []
    n = max(base_limit, 1)
    for _ in range(max(max_steps, 1)):
        limits.append(min(n, cap))
        n = min(n * 2, cap)
    return limits


def _ensure_memory_metadata(metadata: dict | None) -> dict[str, Any]:
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


def _parse_optional_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --- Routes ---

@app.get("/health")
async def health():
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "healthy", "version": "0.1.0"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/v1/memu/health")
async def memu_health_compat():
    return await health()


@app.post("/memories", response_model=Memory)
async def create_memory(req: MemoryCreate, _key: str = Depends(verify_api_key)):
    normalized_metadata = _ensure_memory_metadata(req.metadata)
    embedding = await get_embedding(req.content)
    c_hash = content_hash(req.content)

    async with _tenant_conn(_key) as conn:
        # Check for duplicates (content hash always works; vector similarity only when embedding available)
        if embedding is not None:
            vec = f"vector({EMBEDDING_DIMS})"
            existing = await conn.fetchrow(
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
                json.dumps(normalized_metadata),
            )
        else:
            # Include tenant_id for RLS multi-tenancy
            tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)
            row = await conn.fetchrow(
                """
                INSERT INTO memories (content, embedding, memory_type, agent_id, metadata, parent_id, confidence, content_hash, tenant_id)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                RETURNING *
                """,
                req.content,
                str(embedding) if embedding is not None else None,
                req.memory_type.value,
                req.agent_id,
                json.dumps(normalized_metadata),
                req.parent_id,
                req.confidence,
                c_hash,
                tenant_id,
            )

    memory = _row_to_memory(row)

    # Publish NATS event for every memory write
    if _nats_publisher:
        try:
            await _nats_publisher.publish_memory_written(
                agent_id=req.agent_id or "unknown",
                memory_id=str(memory.id),
                content=req.content,
                memory_type=req.memory_type.value,
                metadata=normalized_metadata,
            )
        except Exception as e:
            logger.warning("NATS publish failed for memory write: %s", e)

    return memory


class MemUAddCompatRequest(BaseModel):
    content: str | None = None
    text: str | None = None
    agent_id: str = "unknown"
    memory_type: MemoryType = MemoryType.observation
    metadata: dict[str, Any] | None = None
    confidence: float = 1.0


class MemUAddCompatResponse(BaseModel):
    ok: bool
    id: str | None = None
    error_code: str | None = None
    message: str


@app.post("/api/v1/memu/add", response_model=MemUAddCompatResponse)
async def memu_add_compat(req: MemUAddCompatRequest, _key: str = Depends(verify_api_key)):
    # Contract v1.1 guardrail: require content; reject legacy text field explicitly.
    if req.content is None and req.text is not None:
        return MemUAddCompatResponse(
            ok=False,
            error_code="INVALID_PAYLOAD",
            message="Use 'content' (string) instead of legacy 'text'.",
        )
    if req.content is None or not req.content.strip():
        return MemUAddCompatResponse(
            ok=False,
            error_code="MISSING_CONTENT",
            message="Missing required field: content",
        )

    memory = await create_memory(
        MemoryCreate(
            content=req.content,
            agent_id=req.agent_id,
            memory_type=req.memory_type,
            metadata=req.metadata,
            confidence=req.confidence,
        ),
        _key=_key,
    )
    return MemUAddCompatResponse(ok=True, id=str(memory.id), message="Memory stored")


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
    embedding = await get_embedding(req.query)
    # Graceful degradation: if embedding service is unavailable, fall back to text search
    if embedding is None:
        logger.warning("Embedding service unavailable for /search, falling back to text search")
        # Delegate to text-based search as fallback
        async with _tenant_conn(_key) as conn:
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
    if req.time_window_start:
        filters.append(f"created_at >= ${idx}")
        params.append(req.time_window_start)
        idx += 1
    if req.time_window_end:
        filters.append(f"created_at <= ${idx}")
        params.append(req.time_window_end)
        idx += 1

    where = (" AND " + " AND ".join(filters)) if filters else ""
    async with _tenant_conn(_key) as conn:
        try:
            await conn.execute("SET LOCAL hnsw.iterative_scan = $1", HNSW_ITERATIVE_SCAN)
            await conn.execute("SET LOCAL hnsw.max_scan_tuples = $1", HNSW_MAX_SCAN_TUPLES)
            await conn.execute("SET LOCAL ivfflat.max_probes = $1", IVFFLAT_MAX_PROBES)
        except Exception:
            # Compatibility with pgvector versions that do not expose these knobs.
            pass

        vec = f"vector({EMBEDDING_DIMS})"
        rows = []
        min_results = max(1, min(req.limit, req.min_results))
        for current_limit in _expansion_schedule(req.limit * 2, req.max_expansion_steps):
            rows = await conn.fetch(
                f"""
                SELECT *, 1 - (embedding <=> $1::{vec}) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL{where}
                ORDER BY embedding <=> $1::{vec}
                LIMIT {current_limit}
                """,
                *params,
            )
            if len(rows) >= min_results:
                break

        # Optional lexical tie-breaker for filtered misses.
        if req.lexical_fallback and len(rows) < min_results:
            lexical_filters = ["content ILIKE $1"]
            lexical_params: list[Any] = [f"%{req.query}%"]
            lidx = 2
            if req.agent_id:
                lexical_filters.append(f"agent_id = ${lidx}")
                lexical_params.append(req.agent_id)
                lidx += 1
            if req.memory_type:
                lexical_filters.append(f"memory_type = ${lidx}")
                lexical_params.append(req.memory_type.value)
                lidx += 1
            if req.min_confidence > 0:
                lexical_filters.append(f"confidence >= ${lidx}")
                lexical_params.append(req.min_confidence)
                lidx += 1

            lexical_rows = await conn.fetch(
                f"""
                SELECT *, 0.45::float8 AS similarity
                FROM memories
                WHERE {' AND '.join(lexical_filters)}
                ORDER BY updated_at DESC
                LIMIT {req.limit}
                """,
                *lexical_params,
            )

            existing_ids = {r["id"] for r in rows}
            for r in lexical_rows:
                if r["id"] not in existing_ids:
                    rows.append(r)
                    existing_ids.add(r["id"])

        # Increment access counts for returned results
        if rows:
            ids = [r["id"] for r in rows]
            await conn.execute(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ANY($1)",
                ids,
            )
    # Apply Graphiti-inspired temporal + entity-aware re-ranking.
    superseded_ids = {
        str(r.get("metadata", {}).get("supersedes"))
        for r in rows
        if isinstance(r.get("metadata"), dict) and r.get("metadata", {}).get("supersedes")
    }

    filtered_rows = []
    now = datetime.now(timezone.utc)
    for row in rows:
        row_id = str(row["id"])
        if row_id in superseded_ids:
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        stale_after_raw = metadata.get("stale_after")
        if isinstance(stale_after_raw, str):
            try:
                stale_after = _parse_optional_iso(stale_after_raw)
                if stale_after and stale_after <= now:
                    continue
            except Exception:
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
    final = results[: req.limit]

    # Audit Trail: Log Search History (reuse the embedding we already computed)
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
                "vector",
                json.dumps({"temporal_weight": req.temporal_weight, "limit": req.limit}),
                emb_str
            )
    except Exception as e:
        # Log at debug level â€” search_history logging failure is non-critical
        logger.debug(f"Failed to log search history: {e}")

    # Publish search event to NATS
    if _nats_publisher:
        try:
            await _nats_publisher.publish_search_logged(
                agent_id=req.agent_id or "unknown",
                query=req.query,
                source="memu_search",
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
    _key: str = Depends(verify_api_key),
):
    req = SearchRequest(
        query=q,
        limit=limit,
        agent_id=agent_id or agent,
        memory_type=_coerce_memory_type(memory_type),
    )
    return await search_memories(req, _key=_key)


class MemUSearchCompatRequest(BaseModel):
    query: str
    agent_id: str | None = None
    limit: int = 10
    memory_type: str | None = None
    time_window_start: str | None = None
    time_window_end: str | None = None
    entity_weight: float = 0.15


@app.post("/api/v1/memu/search")
async def memu_search_post_compat(req: MemUSearchCompatRequest, _key: str = Depends(verify_api_key)):
    search_req = SearchRequest(
        query=req.query,
        limit=req.limit,
        agent_id=req.agent_id,
        memory_type=_coerce_memory_type(req.memory_type),
        time_window_start=_parse_optional_iso(req.time_window_start),
        time_window_end=_parse_optional_iso(req.time_window_end),
        entity_weight=req.entity_weight,
    )
    return await search_memories(search_req, _key=_key)


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


class MemUSearchTextCompatRequest(BaseModel):
    query: str
    agent_id: str | None = None
    memory_type: str | None = None
    limit: int = 10


@app.post("/api/v1/memu/search-text")
async def memu_search_text_post_compat(req: MemUSearchTextCompatRequest, _key: str = Depends(verify_api_key)):
    return await search_text(
        query=req.query,
        agent_id=req.agent_id,
        memory_type=req.memory_type,
        limit=req.limit,
        _key=_key,
    )


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

    imported = 0
    dupes = 0

    for chunk in chunks:
        try:
            embedding = await get_embedding(chunk)
            c_hash = content_hash(chunk)

            async with _tenant_conn(_key) as conn:
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
    tenant_id = getattr(_key, "tenant_id", DEFAULT_TENANT_ID)

    if req.risk_score is None:
        req.risk_score = 25

    source_fingerprint = None
    if req.source or req.source_ref or req.task:
        source_fingerprint = hashlib.sha256(
            f"{req.source or ''}|{req.source_ref or ''}|{req.task}".encode()
        ).hexdigest()[:64]

    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO backlog (
                task,
                priority,
                owner_id,
                lane,
                metadata,
                dependency_id,
                tenant_id,
                risk_score,
                source,
                source_ref,
                project,
                completion_criteria,
                menu_bucket,
                source_fingerprint
            )
            VALUES (
                $1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12, $13, $14
            )
            RETURNING *
            """,
            req.task,
            req.priority.value,
            req.owner_id,
            req.lane,
            json.dumps(req.metadata) if req.metadata else "{}",
            req.dependency_id,
            tenant_id,
            req.risk_score,
            req.source,
            req.source_ref,
            req.project,
            req.completion_criteria,
            req.menu_bucket,
            source_fingerprint,
        )

    task = _row_to_task(row)

    if _nats_publisher:
        try:
            await _nats_publisher.publish_task_drafted(
                agent_id=(req.owner_id or "system"),
                task_id=str(task.id),
                title=task.task,
                source=req.source or "manual",
                risk_score=task.risk_score,
            )
        except Exception as exc:
            logger.warning("Failed to emit TASK_DRAFTED event: %s", exc)

    return task


@app.get("/tasks", response_model=list[Task])
async def list_tasks(
    status: TaskStatus | None = None,
    owner: str | None = None,
    source: str | None = None,
    project: str | None = None,
    review_status: str | None = None,
    min_risk: int | None = None,
    max_risk: int | None = None,
    _key: str = Depends(verify_api_key),
):
    filters = ["tenant_id = $1"]
    params = [str(getattr(_key, "tenant_id", DEFAULT_TENANT_ID))]
    idx = 2
    if status:
        filters.append(f"status = ${idx}")
        params.append(status.value)
        idx += 1
    if owner:
        filters.append(f"owner_id = ${idx}")
        params.append(owner)
        idx += 1
    if source:
        filters.append(f"source = ${idx}")
        params.append(source)
        idx += 1
    if project:
        filters.append(f"project = ${idx}")
        params.append(project)
        idx += 1
    if review_status:
        filters.append(f"review_status = ${idx}")
        params.append(review_status)
        idx += 1
    if min_risk is not None:
        filters.append(f"risk_score >= ${idx}")
        params.append(min_risk)
        idx += 1
    if max_risk is not None:
        filters.append(f"risk_score <= ${idx}")
        params.append(max_risk)
        idx += 1

    where = " WHERE " + " AND ".join(filters)
    async with _tenant_conn(_key) as conn:
        rows = await conn.fetch(
            f"SELECT * FROM backlog{where} ORDER BY priority ASC, risk_score DESC, created_at DESC",
            *params,
        )
    return [_row_to_task(row) for row in rows]


@app.patch("/tasks/{task_id}", response_model=Task)
async def update_task(task_id: UUID, req: TaskUpdate, _key: str = Depends(verify_api_key)):
    updates = []
    values = []
    idx = 1

    if req.owner_id is not None:
        updates.append(f"owner_id = ${idx}")
        values.append(req.owner_id)
        idx += 1

    if req.status is not None:
        updates.append(f"status = ${idx}")
        values.append(req.status.value)
        idx += 1

    if req.lane is not None:
        updates.append(f"lane = ${idx}")
        values.append(req.lane)
        idx += 1

    if req.priority is not None:
        updates.append(f"priority = ${idx}")
        values.append(req.priority.value)
        idx += 1

    if req.risk_score is not None:
        updates.append(f"risk_score = ${idx}")
        values.append(req.risk_score)
        idx += 1

    if req.completion_criteria is not None:
        updates.append(f"completion_criteria = ${idx}")
        values.append(req.completion_criteria)
        idx += 1

    if req.review_status is not None:
        updates.append(f"review_status = ${idx}")
        values.append(req.review_status.value)
        idx += 1

    if req.reviewer_id is not None:
        updates.append(f"reviewer_id = ${idx}")
        values.append(req.reviewer_id)
        idx += 1

    if req.evidence is not None:
        updates.append(f"evidence = ${idx}")
        values.append(req.evidence)
        idx += 1

    if req.metadata is not None:
        updates.append(f"metadata = ${idx}::jsonb")
        values.append(json.dumps(req.metadata))
        idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No update fields provided")

    updates.append(f"updated_at = NOW()")
    values.append(task_id)

    query = "UPDATE backlog SET " + ", ".join(updates) + f" WHERE id = ${idx} RETURNING *"

    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(query, *values)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")

    return _row_to_task(row)


@app.post("/tasks/{task_id}/review", response_model=Task)
async def review_task(task_id: UUID, req: TaskReviewRequest, _key: str = Depends(verify_api_key)):
    now = datetime.now(timezone.utc)
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow("SELECT * FROM backlog WHERE id = $1", task_id)
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")

        existing_notes = row["review_notes"] or ""
        existing_retry = row["retry_count"] or 0
        notes = f"[{now.isoformat()}] reviewer={req.reviewer_id} decision={req.decision.value} notes={req.notes}"
        combined_notes = "\n".join([n for n in [existing_notes, notes] if n])

        if req.decision.value == "approve":
            new_status = "done"
            new_review_status = "passed"
            new_refine_status = "approved"
            new_retry = existing_retry
        elif req.decision.value == "block":
            new_status = "blocked"
            new_review_status = "blocked"
            new_refine_status = "needs_revision"
            new_retry = existing_retry
        else:
            new_status = "pending"
            new_review_status = "needs_input"
            new_refine_status = "needs_revision"
            new_retry = existing_retry + 1

        updated = await conn.fetchrow(
            """
            UPDATE backlog
            SET
                status = $2,
                review_status = $3,
                reviewer_id = $4,
                reviewed_at = $5,
                review_notes = $6,
                retry_count = $7,
                refine_status = $8,
                refined_at = CASE WHEN $3 = 'passed' THEN $5 ELSE refined_at END,
                updated_at = NOW()
            WHERE id = $1
            RETURNING *
            """,
            task_id,
            new_status,
            new_review_status,
            req.reviewer_id,
            now,
            combined_notes,
            new_retry,
            new_refine_status,
        )

    task = _row_to_task(updated)

    if _nats_publisher:
        try:
            if req.decision.value == "approve":
                await _nats_publisher.publish_task_completed(
                    agent_id=row["owner_id"] or req.reviewer_id,
                    task_id=str(task.id),
                    title=task.task,
                    evidence=task.evidence or "",
                    source=task.source or "review",
                )
            elif req.decision.value in {"needs_info", "rework"}:
                await _nats_publisher.publish_task_failed(
                    agent_id=row["owner_id"] or req.reviewer_id,
                    task_id=str(task.id),
                    title=task.task,
                    reason=f"{req.decision.value}: {req.notes}",
                    source=row["source"] or "review",
                    evidence=task.evidence or "",
                )
        except Exception as exc:
            logger.warning("Failed to emit task review event: %s", exc)

    return task
@app.get("/api/v1/gateway-leases/{lease_key:path}", response_model=GatewayLease)
async def get_gateway_lease(lease_key: str, _key: str = Depends(verify_api_key)):
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM gateway_topic_leases WHERE lease_key = $1",
            lease_key,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Lease not found")
    return _row_to_gateway_lease(row)


@app.post("/api/v1/gateway-leases/acquire", response_model=GatewayLeaseAcquireResponse)
async def acquire_gateway_lease(req: GatewayLeaseAcquireRequest, _key: str = Depends(verify_api_key)):
    now = datetime.now(timezone.utc)
    expires_at = now.replace(microsecond=0) + timedelta(seconds=req.ttl_seconds)
    status = "claimed"

    async with _tenant_conn(_key) as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM gateway_topic_leases WHERE lease_key = $1 FOR UPDATE",
                req.lease_key,
            )
            if row is None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO gateway_topic_leases (
                        lease_key, owner_gateway, backup_gateway, lease_expires_at,
                        last_message_id, context_digest, task_state, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
                    RETURNING *
                    """,
                    req.lease_key,
                    req.gateway_id,
                    req.backup_gateway,
                    expires_at,
                    req.last_message_id,
                    req.context_digest,
                    json.dumps(req.task_state or {}),
                    json.dumps(req.metadata or {}),
                )
            else:
                if row["owner_gateway"] == req.gateway_id:
                    status = "renewed"
                elif row["lease_expires_at"] > now:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": "Lease is currently held by another gateway",
                            "owner_gateway": row["owner_gateway"],
                            "lease_expires_at": row["lease_expires_at"].isoformat(),
                            "version": row["version"],
                        },
                    )
                else:
                    status = "stolen"

                row = await conn.fetchrow(
                    """
                    UPDATE gateway_topic_leases
                    SET owner_gateway = $2,
                        backup_gateway = COALESCE($3, backup_gateway),
                        lease_expires_at = $4,
                        last_message_id = COALESCE($5, last_message_id),
                        context_digest = COALESCE($6, context_digest),
                        task_state = CASE WHEN $7::jsonb = '{}'::jsonb THEN task_state ELSE $7::jsonb END,
                        metadata = metadata || $8::jsonb,
                        version = version + 1
                    WHERE lease_key = $1
                    RETURNING *
                    """,
                    req.lease_key,
                    req.gateway_id,
                    req.backup_gateway,
                    expires_at,
                    req.last_message_id,
                    req.context_digest,
                    json.dumps(req.task_state or {}),
                    json.dumps(req.metadata or {}),
                )

    lease = _row_to_gateway_lease(row)
    await _publish_gateway_lease_event(status, lease)
    return GatewayLeaseAcquireResponse(status=status, lease=lease)


@app.post("/api/v1/gateway-leases/renew", response_model=GatewayLeaseAcquireResponse)
async def renew_gateway_lease(req: GatewayLeaseRenewRequest, _key: str = Depends(verify_api_key)):
    now = datetime.now(timezone.utc)
    expires_at = now.replace(microsecond=0) + timedelta(seconds=req.ttl_seconds)
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            """
            UPDATE gateway_topic_leases
            SET lease_expires_at = $3,
                last_message_id = COALESCE($4, last_message_id),
                last_reply_id = COALESCE($5, last_reply_id),
                context_digest = COALESCE($6, context_digest),
                task_state = CASE WHEN $7::jsonb = '{}'::jsonb THEN task_state ELSE $7::jsonb END,
                metadata = metadata || $8::jsonb,
                version = version + 1
            WHERE lease_key = $1
              AND owner_gateway = $2
            RETURNING *
            """,
            req.lease_key,
            req.gateway_id,
            expires_at,
            req.last_message_id,
            req.last_reply_id,
            req.context_digest,
            json.dumps(req.task_state or {}),
            json.dumps(req.metadata or {}),
        )
    if not row:
        raise HTTPException(status_code=409, detail="Lease renew rejected: caller is not current owner")
    lease = _row_to_gateway_lease(row)
    await _publish_gateway_lease_event("renewed", lease)
    return GatewayLeaseAcquireResponse(status="renewed", lease=lease)


@app.post("/api/v1/gateway-leases/release")
async def release_gateway_lease(req: GatewayLeaseReleaseRequest, _key: str = Depends(verify_api_key)):
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            "DELETE FROM gateway_topic_leases WHERE lease_key = $1 AND owner_gateway = $2 RETURNING *",
            req.lease_key,
            req.gateway_id,
        )
    if not row:
        raise HTTPException(status_code=409, detail="Lease release rejected: caller is not current owner")
    lease = _row_to_gateway_lease(row)
    await _publish_gateway_lease_event("released", lease)
    return {"ok": True, "status": "released", "lease": lease.model_dump(mode="json")}


# --- Graph / Cypher Queries ---

class CypherRequest(BaseModel):
    query: str
    graph_name: str = "memu_graph"

@app.post("/api/v1/memu/cypher")
async def execute_cypher(req: CypherRequest, _key: str = Depends(verify_api_key)):
    """Execute raw Cypher queries against the memU knowledge graph."""
    graph_name = _validate_graph_identifier(req.graph_name)
    async with pool.acquire() as conn:
        try:
            await conn.execute("SET search_path = ag_catalog, \"$user\", public")
            # Example query: MATCH (v:Memory) RETURN v
            cypher_sql = f"SELECT * FROM cypher('{graph_name}', $$ {req.query} $$) AS (result agtype);"
            rows = await conn.fetch(cypher_sql)
            return {"ok": True, "results": [dict(r) for r in rows]}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/memu/graph/neighbors/{memory_id}")
async def get_graph_neighbors(memory_id: UUID, _key: str = Depends(verify_api_key)):
    """Traverse graph to find 1st degree neighbors of a memory."""
    async with pool.acquire() as conn:
        try:
            await conn.execute("SET search_path = ag_catalog, \"$user\", public")
            cypher_sql = f"""
            SELECT * FROM cypher('memu_graph', $$ 
                MATCH (m:Memory {{id: "{memory_id}"}})-[r]-(neighbor:Memory)
                RETURN type(r) as rel_type, neighbor
            $$) AS (rel_type agtype, neighbor agtype);
            """
            rows = await conn.fetch(cypher_sql)
            return {"ok": True, "neighbors": [dict(r) for r in rows]}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

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

class DedupeResponse(BaseModel):
    deleted_count: int
    merged_access_count: int
    dry_run: bool

@app.post("/api/v1/memu/dedupe", response_model=DedupeResponse)
async def dedupe_memories(
    dry_run: bool = False,
    _key: str = Depends(verify_api_key),
):
    """Find and merge identical memories by content_hash to reduce context bloat."""
    async with _tenant_conn(_key) as conn:
        rows = await conn.fetch('''
            SELECT content_hash, array_agg(id ORDER BY created_at ASC) as ids, sum(access_count) as total_access
            FROM memories
            GROUP BY content_hash
            HAVING count(id) > 1
        ''')
        
        deleted = 0
        merged = 0
        
        for row in rows:
            ids = row['ids']
            keep_id = ids[0]
            drop_ids = ids[1:]
            total_access = row['total_access']
            
            if not dry_run:
                # Add access_count to the kept id
                await conn.execute('UPDATE memories SET access_count = $1, updated_at = NOW() WHERE id = $2', total_access, keep_id)
                # Relink any memory_links pointing to drop_ids to keep_id
                await conn.execute('UPDATE memory_links SET source_id = $1 WHERE source_id = ANY($2) AND source_id != $1', keep_id, drop_ids)
                await conn.execute('UPDATE memory_links SET target_id = $1 WHERE target_id = ANY($2) AND target_id != $1', keep_id, drop_ids)
                # Delete the redundant ones
                await conn.execute('DELETE FROM memories WHERE id = ANY($1)', drop_ids)
                
            deleted += len(drop_ids)
            merged += total_access
            
    return DedupeResponse(deleted_count=deleted, merged_access_count=merged, dry_run=dry_run)

class HygieneCleanupResponse(BaseModel):
    deleted_history_count: int
    dry_run: bool

@app.post("/api/v1/memu/hygiene/cleanup", response_model=HygieneCleanupResponse)
async def history_cleanup(
    days_old: int = 30,
    dry_run: bool = False,
    _key: str = Depends(verify_api_key),
):
    """Periodic cleanup of old search history and stale logs for operational hygiene."""
    async with _tenant_conn(_key) as conn:
        if dry_run:
            val = await conn.fetchval("SELECT count(*) FROM search_history WHERE created_at < NOW() - INTERVAL '1 day' * $1", days_old)
            return HygieneCleanupResponse(deleted_history_count=val or 0, dry_run=True)
        
        res = await conn.execute("DELETE FROM search_history WHERE created_at < NOW() - INTERVAL '1 day' * $1", days_old)
        deleted = int(res.split()[1]) if res.startswith("DELETE") else 0
        return HygieneCleanupResponse(deleted_history_count=deleted, dry_run=False)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
