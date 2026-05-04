"""memU API â€” FastAPI application."""

from __future__ import annotations
from datetime import datetime, timezone, timedelta

import hashlib
import json
from memu.web_search_ingest import ingest_web_search
import os
import logging
import signal
from contextlib import asynccontextmanager
from typing import Any, Optional
from uuid import UUID
import re

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import JSONResponse
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
from memu.core_memory import (
    Block,
    CoreMemoryManager,
    CASConflictError,
)
from memu.semantic_router import SemanticRouter, SearchTarget
from memu.nats_publisher import NATSEventPublisher
from memu.models import (
    BulkImportRequest,
    BulkImportResponse,
    ChatRequest,
    ChatResponse,
    ForensicRecallItem,
    ForensicRecallRequest,
    ForensicRecallResponse,
    GatewayLease,
    GatewayLeaseAcquireRequest,
    GatewayLeaseAcquireResponse,
    GatewayLeaseReleaseRequest,
    GatewayLeaseRenewRequest,
    Memory,
    MemoryCreate,
    MemoryKind,
    MemoryType,
    RecallMode,
    ReflectionAction,
    ReflectionActionRequest,
    ReflectionActionResponse,
    ReflectionProposal,
    ReflectionProposalCreate,
    SearchRequest,
    SearchResult,
    Task,
    TaskCreate,
    TaskStatus,
    TaskUpdate,
    TaskReviewRequest,
)
from memu.schema_contract import compute_canonical_payload_hash
from memu.reflection import compute_proposal_expiry, is_proposal_expired

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
EMBEDDING_API_BASE = os.environ.get("EMBEDDING_API_BASE", "https://api.openai.com")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "1536"))
DEDUP_THRESHOLD = float(os.environ.get("DEDUP_THRESHOLD", "0.95"))
DECAY_RATE = float(os.environ.get("DECAY_RATE", "0.01"))
HNSW_ITERATIVE_SCAN = os.environ.get("HNSW_ITERATIVE_SCAN", "strict_order")
HNSW_MAX_SCAN_TUPLES = int(os.environ.get("HNSW_MAX_SCAN_TUPLES", "20000"))
IVFFLAT_MAX_PROBES = int(os.environ.get("IVFFLAT_MAX_PROBES", "10"))

# --- Globals ---

pool: asyncpg.Pool | None = None
_nats_cluster: NATSClusterManager | None = None
_nats_publisher: NATSEventPublisher | None = None
_core_memory_mgr: CoreMemoryManager | None = None
_implicit_profiler: Any = None  # ImplicitProfiler (lazy import)
_semantic_router: SemanticRouter | None = None
_embedding_schema_ok: bool = True  # Set False when configured dims ≠ schema dims
is_shutting_down: bool = False  # Set True on SIGTERM; triggers 503 for new requests
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, _fastembed_model, _nats_cluster, _nats_publisher, _core_memory_mgr, _implicit_profiler
    # Conservative pool size: allows 4 pods × 5 = 20 total connections during
    # rolling deploys. statement_cache_size=0 for PgBouncer compatibility.
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=10,
        max_inactive_connection_lifetime=30,
        statement_cache_size=0,
    )
    
    # Run DB migrations
    try:
        await run_migrations(pool)
    except Exception as e:
        logger.error(f"Migration startup failed: {e}")

    # Verify embedding contract: configured dims must match schema
    global _embedding_schema_ok
    try:
        from memu.embedding_contract import verify_embedding_schema, resolve_embedding_api_base
        resolve_embedding_api_base()  # logs alias warning if EMBEDDING_BASE_URL is used
        _embedding_schema_ok = await verify_embedding_schema(pool, EMBEDDING_DIMS)
    except Exception as e:
        logger.error("Embedding contract check failed: %s. Semantic recall may be degraded.", e)
        _embedding_schema_ok = False
    
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

        # Initialize Core Memory Manager (NATS KV)
        try:
            _core_memory_mgr = CoreMemoryManager(_nats_cluster.jetstream)
            await _core_memory_mgr.ensure_bucket()
            logger.info("Core Memory Manager initialized (NATS KV)")
        except Exception as e:
            logger.warning("Core Memory Manager init failed: %s", e)
            _core_memory_mgr = None

        # Start Implicit Profiling Daemon
        try:
            from memu.implicit_profiler import ImplicitProfiler
            if _core_memory_mgr:
                _implicit_profiler = ImplicitProfiler(_nats_cluster, _core_memory_mgr)
                asyncio.create_task(_implicit_profiler.start())
                logger.info("Implicit Profiler daemon started")
        except Exception as e:
            logger.warning("Implicit Profiler startup failed: %s", e)

    except Exception as e:
        logger.warning("NATS connection failed (API will work without events): %s", e)
        _nats_cluster = None
        _nats_publisher = None
        _core_memory_mgr = None

    # Initialize Semantic Router (no NATS dependency)
    global _semantic_router
    try:
        _semantic_router = SemanticRouter()
        ok = await _semantic_router.initialize(get_embedding)
        if not ok:
            logger.warning("Semantic Router init failed — falling back to hybrid search")
            _semantic_router = None
        else:
            logger.info("Semantic Router initialized")
    except Exception as e:
        logger.warning("Semantic Router init error: %s", e)
        _semantic_router = None

    yield

    # --- Shutdown (triggered by SIGTERM / SIGINT) ---
    logger.info("SIGTERM received — beginning graceful shutdown")
    global is_shutting_down
    is_shutting_down = True

    # Release PostgreSQL gateway leases so other pods are not blocked
    if pool:
        try:
            from memu.lane_lock import release_all_my_leases
            await release_all_my_leases(pool)
        except Exception as exc:
            logger.warning("Lease release during shutdown failed: %s", exc)

    if _implicit_profiler:
        try:
            await _implicit_profiler.stop()
        except Exception:
            pass
    if _nats_cluster:
        try:
            await _nats_cluster.active_connection.drain()
        except Exception:
            pass
        await _nats_cluster.close()
    if pool:
        await pool.close()
    logger.info("Graceful shutdown complete")


app = FastAPI(
    title="memU",
    description="Free, open-source shared memory for AI agents.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(temporal_router, tags=["Async Workflows"])


@app.middleware("http")
async def shutdown_guard(request: Request, call_next):
    """Return 503 for new requests when the pod is draining (SIGTERM received).

    This prevents the load balancer from routing new traffic to a dying pod
    during Railway rolling deploys.
    """
    if is_shutting_down:
        return JSONResponse(
            status_code=503,
            content={"detail": "Server is shutting down"},
        )
    return await call_next(request)


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


def _cypher_string_literal(value: str) -> str:
    """Wrap *value* as a safe Cypher double-quoted string literal.

    Escapes backslashes and double-quotes so the result can be embedded
    directly inside a Cypher string context (e.g. {id: "<value>"}).
    UUID strings are trivially safe, but routing through this helper
    ensures consistent, auditable escaping for all Cypher string values.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --- Embedding ---

async def get_embedding(text: str) -> list[float] | None:
    """Get embedding vector via NATS KV cache or external Serverless API.

    All ML inference is offloaded to an external API (e.g., OpenAI text-embedding-3-small).
    No ML models are loaded in the Gateway pod. NATS KV provides L1 semantic caching.
    """
    from memu.embeddings_client import get_embedding as _get_embedding
    nc = _nats_cluster.active_connection if _nats_cluster else None
    return await _get_embedding(text, nc=nc)


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
        memory_kind=_record_value(row, "memory_kind", "learning"),
        agent_id=row["agent_id"],
        metadata=metadata,
        parent_id=row["parent_id"],
        confidence=row["confidence"],
        access_count=row["access_count"],
        decay_score=row["decay_score"],
        salience_score=float(_record_value(row, "salience_score", 0.5) or 0.5),
        searchable=bool(_record_value(row, "searchable", True)),
        review_status=_record_value(row, "review_status", None),
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


def _record_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _coerce_uuid_list(values: Any) -> list[UUID]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        values = [values]

    seen: set[UUID] = set()
    normalized: list[UUID] = []
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = value if isinstance(value, UUID) else UUID(str(value))
        except (TypeError, ValueError):
            continue
        if parsed not in seen:
            seen.add(parsed)
            normalized.append(parsed)
    return normalized


def _extract_superseding_targets(
    metadata: dict[str, Any] | None,
    supersedes: UUID | None = None,
    invalidates: list[UUID] | None = None,
) -> list[UUID]:
    payload = metadata or {}
    targets: list[UUID] = []
    if supersedes is not None:
        targets.append(supersedes)
    if invalidates:
        targets.extend(invalidates)
    targets.extend(_coerce_uuid_list(payload.get("supersedes")))
    targets.extend(_coerce_uuid_list(payload.get("invalidates")))
    targets.extend(_coerce_uuid_list(payload.get("invalidated_ids")))
    return _coerce_uuid_list(targets)


def _apply_superseding_metadata(metadata: dict[str, Any], superseded_ids: list[UUID]) -> dict[str, Any]:
    payload = dict(metadata)
    if not superseded_ids:
        return payload

    serialized = [str(value) for value in superseded_ids]
    payload["supersedes"] = serialized[0] if len(serialized) == 1 else serialized
    payload["invalidates"] = serialized
    return payload


def _collect_invalidated_ids(rows: list[Any]) -> set[str]:
    invalidated_ids: set[str] = set()
    for row in rows:
        metadata = _record_value(row, "metadata", {})
        if not isinstance(metadata, dict):
            continue
        for key in ("supersedes", "invalidates", "invalidated_ids"):
            invalidated_ids.update(str(value) for value in _coerce_uuid_list(metadata.get(key)))
    return invalidated_ids


def _is_current_memory_row(row: Any, now: datetime | None = None) -> bool:
    current_time = now or datetime.now(timezone.utc)

    valid_to = _record_value(row, "valid_to")
    if isinstance(valid_to, str):
        try:
            valid_to = _parse_optional_iso(valid_to)
        except Exception:
            valid_to = None
    if valid_to is not None and valid_to <= current_time:
        return False

    metadata = _record_value(row, "metadata", {})
    if not isinstance(metadata, dict):
        return True

    stale_after_raw = metadata.get("stale_after")
    if isinstance(stale_after_raw, str):
        try:
            stale_after = _parse_optional_iso(stale_after_raw)
            if stale_after and stale_after <= current_time:
                return False
        except Exception:
            pass

    if metadata.get("invalidated_by") and metadata.get("invalidated_at"):
        return False

    return True


def _filter_current_search_rows(rows: list[Any], now: datetime | None = None) -> list[Any]:
    invalidated_ids = _collect_invalidated_ids(rows)
    filtered_rows: list[Any] = []
    for row in rows:
        row_id = _record_value(row, "id")
        if row_id is not None and str(row_id) in invalidated_ids:
            continue
        if not _is_current_memory_row(row, now=now):
            continue
        filtered_rows.append(row)
    return filtered_rows


async def _apply_superseding_operations(conn: Any, new_memory_id: UUID, superseded_ids: list[UUID]) -> None:
    if not superseded_ids:
        return

    now = datetime.now(timezone.utc)
    await conn.execute(
        """
        UPDATE memories
        SET valid_to = COALESCE(valid_to, $2),
            metadata = COALESCE(metadata, '{}'::jsonb)
                || jsonb_build_object('invalidated_by', $1::text, 'invalidated_at', $2::text),
            updated_at = NOW()
        WHERE id = ANY($3::uuid[])
          AND id <> $1
          AND (valid_to IS NULL OR valid_to > $2)
        """,
        new_memory_id,
        now,
        superseded_ids,
    )
    await conn.execute(
        """
        INSERT INTO memory_links (source_id, target_id, relationship, strength, metadata)
        SELECT $1, superseded_id, 'supersedes', 1.0, jsonb_build_object('temporal', true)
        FROM unnest($2::uuid[]) AS superseded_id
        WHERE superseded_id <> $1
        ON CONFLICT (source_id, target_id, relationship)
        DO UPDATE SET
            strength = GREATEST(memory_links.strength, EXCLUDED.strength),
            metadata = memory_links.metadata || EXCLUDED.metadata,
            last_accessed = NOW()
        """,
        new_memory_id,
        superseded_ids,
    )


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


def _rerank_lost_in_middle(results: list) -> list:
    """'Pre-Frontal Cortex' re-ranking to mitigate LLM 'Lost in the Middle' bias.

    LLMs naturally attend more strongly to items at the very top and very bottom
    of their context window, and tend to ignore the middle. This re-arranges the
    result list so that:
      - Top 20% most salient items → placed at the VERY TOP
      - Next 20% most salient items → placed at the VERY BOTTOM
      - Remaining 60%               → placed in the MIDDLE

    The input list must already be sorted by ``final_score`` descending.
    """
    n = len(results)
    if n <= 2:
        return results  # nothing to re-arrange

    top_count = max(1, n // 5)       # 20%
    bottom_count = max(1, n // 5)    # 20%
    # Ensure we don't over-allocate for very small lists
    if top_count + bottom_count >= n:
        return results

    top = results[:top_count]
    bottom = results[top_count:top_count + bottom_count]
    middle = results[top_count + bottom_count:]

    return top + middle + bottom


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
    # ABAC: persist allowed_roles in metadata for vector-level access control
    normalized_metadata["allowed_roles"] = req.allowed_roles or ["*"]
    superseded_ids = _extract_superseding_targets(
        normalized_metadata,
        supersedes=req.supersedes,
        invalidates=req.invalidates,
    )
    normalized_metadata = _apply_superseding_metadata(normalized_metadata, superseded_ids)

    memory_kind = req.memory_kind.value
    is_evidence = (memory_kind == MemoryKind.evidence.value)
    idempotency_key = req.idempotency_key or None

    # Canonical payload hash — only computed for evidence writes with an idempotency key.
    # Excludes transport-only fields so retries with the same payload produce the same hash.
    canonical_hash: str | None = None
    if is_evidence and idempotency_key:
        canonical_hash = compute_canonical_payload_hash(
            content=req.content,
            memory_type=req.memory_type.value,
            agent_id=req.agent_id,
            metadata=normalized_metadata,
        )

    embedding = await get_embedding(req.content)
    c_hash = content_hash(req.content)
    tenant_id = getattr(_key, 'tenant_id', DEFAULT_TENANT_ID)

    async with _tenant_conn(_key) as conn:
        # --- Evidence Memory: idempotency check ---
        # Evidence rows are never content-deduped across distinct events.
        # Dedup is handled exclusively via (tenant_id, idempotency_key).
        if is_evidence and idempotency_key:
            existing_idem = await conn.fetchrow(
                """
                SELECT id, canonical_payload_hash
                FROM memories
                WHERE tenant_id = $1 AND idempotency_key = $2
                LIMIT 1
                """,
                tenant_id,
                idempotency_key,
            )
            if existing_idem:
                stored_hash = existing_idem["canonical_payload_hash"]
                if stored_hash == canonical_hash:
                    # Exact replay — return the original memory unchanged
                    row = await conn.fetchrow(
                        "SELECT * FROM memories WHERE id = $1",
                        existing_idem["id"],
                    )
                    return _row_to_memory(row)
                else:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "idempotency_conflict",
                            "message": (
                                "An Evidence Memory with this idempotency_key already exists "
                                "but its canonical payload hash differs from this request."
                            ),
                            "existing_id": str(existing_idem["id"]),
                        },
                    )

        # --- Learning Memory: content-hash / vector dedup ---
        # Evidence Memory skips this block — distinct task/session/gateway events
        # must remain separate records even when their content is identical.
        row = None
        if not is_evidence:
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

        # --- Insert new memory (evidence always reaches here; learning only when no dedup hit) ---
        if row is None:
            row = await conn.fetchrow(
                """
                INSERT INTO memories (
                    content, embedding, memory_type, memory_kind,
                    agent_id, metadata, parent_id, confidence,
                    content_hash, tenant_id, idempotency_key, canonical_payload_hash
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12)
                RETURNING *
                """,
                req.content,
                str(embedding) if embedding is not None else None,
                req.memory_type.value,
                memory_kind,
                req.agent_id,
                json.dumps(normalized_metadata),
                req.parent_id,
                req.confidence,
                c_hash,
                tenant_id,
                idempotency_key,
                canonical_hash,
            )

        superseded_ids = [value for value in superseded_ids if value != row["id"]]
        await _apply_superseding_operations(conn, row["id"], superseded_ids)

    memory = _row_to_memory(row)

    # Process Graph-Lite relationships if provided
    if req.relationships:
        async with _tenant_conn(_key) as conn:
            for rel in req.relationships:
                try:
                    # If target_memory_id is provided, create a direct link
                    if rel.target_memory_id:
                        await conn.execute(
                            """
                            INSERT INTO memory_links (source_id, target_id, relationship, strength, metadata)
                            VALUES ($1, $2, $3, $4, $5::jsonb)
                            ON CONFLICT (source_id, target_id, relationship)
                            DO UPDATE SET strength = LEAST(1.0, memory_links.strength + 0.1), last_accessed = NOW()
                            """,
                            memory.id,
                            rel.target_memory_id,
                            rel.relationship_type,
                            rel.strength,
                            json.dumps({"entity": rel.entity}),
                        )
                    else:
                        # Store entity reference in metadata for future linking
                        # This allows semantic search to find related memories later
                        await conn.execute(
                            """
                            UPDATE memories
                            SET metadata = metadata || $2::jsonb
                            WHERE id = $1
                            """,
                            memory.id,
                            json.dumps({
                                "entities": [
                                    {
                                        "name": rel.entity,
                                        "relationship_type": rel.relationship_type,
                                        "strength": rel.strength
                                    }
                                ]
                            }),
                        )
                except Exception as e:
                    logger.warning(f"Failed to create relationship link: {e}")

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
    memory_kind: MemoryKind = MemoryKind.learning
    metadata: dict[str, Any] | None = None
    confidence: float = 1.0
    idempotency_key: str | None = None


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

    try:
        memory = await create_memory(
            MemoryCreate(
                content=req.content,
                agent_id=req.agent_id,
                memory_type=req.memory_type,
                memory_kind=req.memory_kind,
                metadata=req.metadata,
                confidence=req.confidence,
                idempotency_key=req.idempotency_key,
            ),
            _key=_key,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            return MemUAddCompatResponse(
                ok=False,
                id=detail.get("existing_id"),
                error_code="IDEMPOTENCY_CONFLICT",
                message=detail.get("message", "Idempotency conflict"),
            )
        raise
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


@app.get("/api/v1/memu/stats")
async def get_stats(_key: str = Depends(verify_api_key)):
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM memories")
        by_type_rows = await conn.fetch("SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type")
        by_agent_rows = await conn.fetch("SELECT agent_id, COUNT(*) FROM memories GROUP BY agent_id")
    
    return {
        "total": total,
        "by_type": {row["memory_type"]: row["count"] for row in by_type_rows},
        "by_agent": {row["agent_id"]: row["count"] for row in by_agent_rows}
    }

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
            filters = ["content ILIKE $1", "valid_to IS NULL"]
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
                salience_score=float(row.get("salience_score", 0.5) or 0.5),
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
    
    filters = ["valid_to IS NULL"]
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

    # ABAC: hard WHERE clause — filter by agent_roles BEFORE ANN similarity
    # Uses JSONB array overlap (&&) to intersect caller roles with allowed_roles.
    # Memories with '*' in allowed_roles are accessible to all agents.
    if req.agent_roles:
        filters.append(
            f"(metadata->'allowed_roles' @> '\"*\"'::jsonb "
            f"OR metadata->'allowed_roles' && ${idx}::jsonb)"
        )
        params.append(json.dumps(req.agent_roles))
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
            lexical_filters = ["content ILIKE $1", "valid_to IS NULL"]
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
    filtered_rows = _filter_current_search_rows(rows)

    results = []
    for row in filtered_rows:
        score = compute_final_score(
            similarity=row["similarity"],
            created_at=row["created_at"],
            access_count=row["access_count"],
            decay_rate=DECAY_RATE,
            temporal_weight=req.temporal_weight,
            salience_score=float(row.get("salience_score", 0.5) or 0.5),
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

    # "Pre-Frontal Cortex" re-ranking: mitigate LLM "Lost in the Middle" bias.
    # Top 20% → top, next 20% → bottom, remaining 60% → middle.
    final = _rerank_lost_in_middle(final)

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


# ---------------------------------------------------------------------------
# Recall contract helpers
# ---------------------------------------------------------------------------

# review_status values that are eligible for default Learning recall.
_LEARNING_RECALL_REVIEW_STATUSES: tuple[str, ...] = (
    "accepted",
    "legacy",
    "accepted_by_timeout",
)


def _has_role_access(allowed_roles: list[str], agent_roles: list[str] | None) -> bool:
    """Return True if the caller's roles grant access to the memory.

    A memory with ['*'] in allowed_roles is accessible to everyone.
    When agent_roles is None, no role filtering is applied (trusted caller).
    """
    if agent_roles is None:
        return True
    if "*" in allowed_roles:
        return True
    return bool(set(allowed_roles) & set(agent_roles))


def _build_forensic_item(
    row: Any,
    include_content: bool,
    agent_roles: list[str] | None,
) -> ForensicRecallItem:
    """Construct a ForensicRecallItem from a DB row, applying redaction rules."""
    metadata: dict[str, Any] = row.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    allowed_roles: list[str] = metadata.get("allowed_roles") or ["*"]

    # Determine redaction
    redacted = False
    redaction_reason: str | None = None
    content: str | None = row.get("content")

    if not include_content:
        redacted = True
        redaction_reason = "content_excluded_by_request"
        content = None
    elif not _has_role_access(allowed_roles, agent_roles):
        redacted = True
        redaction_reason = "role_access_denied"
        content = None

    # Extract provenance links from metadata (source evidence IDs, correction chains)
    provenance_links: list[str] = []
    for key in ("source_evidence_ids", "superseded_ids", "correction_of"):
        val = metadata.get(key)
        if isinstance(val, list):
            provenance_links.extend(str(v) for v in val)
        elif isinstance(val, str) and val:
            provenance_links.append(val)

    # artifact_refs may be a list or a comma-separated string in metadata
    artifact_refs: list[str] = []
    raw_refs = metadata.get("artifact_refs")
    if isinstance(raw_refs, list):
        artifact_refs = [str(r) for r in raw_refs]
    elif isinstance(raw_refs, str) and raw_refs:
        artifact_refs = [raw_refs]

    event_at_raw = metadata.get("event_at")
    event_at: datetime | None = None
    if event_at_raw:
        try:
            event_at = datetime.fromisoformat(str(event_at_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return ForensicRecallItem(
        evidence_id=row["id"],
        memory_kind=row.get("memory_kind") or "evidence",
        memory_type=row.get("memory_type") or "unknown",
        content=content,
        redacted=redacted,
        redaction_reason=redaction_reason,
        event_type=metadata.get("event_type"),
        event_at=event_at,
        task_id=metadata.get("task_id"),
        session_id=metadata.get("session_id"),
        gateway_id=metadata.get("gateway_id"),
        agent_id=row.get("agent_id") or "",
        source=metadata.get("source"),
        source_ref=metadata.get("source_ref"),
        artifact_refs=artifact_refs,
        allowed_roles=allowed_roles,
        provenance_links=provenance_links,
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Learning Recall endpoint — default recall mode, Learning Memory only
# ---------------------------------------------------------------------------

@app.post("/api/v1/recall", response_model=list[SearchResult])
async def learning_recall(req: SearchRequest, _key: str = Depends(verify_api_key)):
    """Default recall: returns Learning Memory only (accepted, legacy, accepted_by_timeout).

    Evidence Memory is never included.  Supports vector, lexical, and temporal fusion.
    This is the correct endpoint for injecting reusable guidance into OpenClaw context.
    """
    embedding = await get_embedding(req.query)

    # Learnnig-only hard filters appended to every query in this endpoint.
    learning_kind_sql = "memory_kind = 'learning'"
    review_statuses_sql = (
        "review_status IN ('accepted', 'legacy', 'accepted_by_timeout')"
    )

    async with _tenant_conn(_key) as conn:
        if embedding is None:
            logger.warning("Embedding unavailable for /api/v1/recall; falling back to lexical search")
            filters = [
                "content ILIKE $1",
                "valid_to IS NULL",
                learning_kind_sql,
                review_statuses_sql,
            ]
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
                    similarity=0.5,
                    created_at=row["created_at"],
                    access_count=row["access_count"],
                    decay_rate=DECAY_RATE,
                    temporal_weight=req.temporal_weight,
                    salience_score=float(row.get("salience_score", 0.5) or 0.5),
                )
                results.append(SearchResult(memory=_row_to_memory(row), similarity=0.0, final_score=score))
            results.sort(key=lambda r: r.final_score, reverse=True)
            return results[: req.limit]

        # Vector search path
        filters = [
            "valid_to IS NULL",
            learning_kind_sql,
            review_statuses_sql,
        ]
        params = [str(embedding)]
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
        if req.agent_roles:
            filters.append(
                f"(metadata->'allowed_roles' @> '\"*\"'::jsonb "
                f"OR metadata->'allowed_roles' && ${idx}::jsonb)"
            )
            params.append(json.dumps(req.agent_roles))
            idx += 1

        where = " AND ".join(filters)
        vec = f"vector({EMBEDDING_DIMS})"
        rows = []
        min_results = max(1, min(req.limit, req.min_results))
        for current_limit in _expansion_schedule(req.limit * 2, req.max_expansion_steps):
            rows = await conn.fetch(
                f"""
                SELECT *, 1 - (embedding <=> $1::{vec}) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL AND {where}
                ORDER BY embedding <=> $1::{vec}
                LIMIT {current_limit}
                """,
                *params,
            )
            if len(rows) >= min_results:
                break

        # Lexical fallback when vector results are sparse
        if req.lexical_fallback and len(rows) < min_results:
            lex_filters = [
                "content ILIKE $1",
                "valid_to IS NULL",
                learning_kind_sql,
                review_statuses_sql,
            ]
            lex_params: list[Any] = [f"%{req.query}%"]
            lidx = 2
            if req.agent_id:
                lex_filters.append(f"agent_id = ${lidx}")
                lex_params.append(req.agent_id)
                lidx += 1
            if req.memory_type:
                lex_filters.append(f"memory_type = ${lidx}")
                lex_params.append(req.memory_type.value)
                lidx += 1
            lex_rows = await conn.fetch(
                f"""
                SELECT *, 0.45::float8 AS similarity
                FROM memories
                WHERE {" AND ".join(lex_filters)}
                ORDER BY updated_at DESC
                LIMIT {req.limit}
                """,
                *lex_params,
            )
            existing_ids = {r["id"] for r in rows}
            for r in lex_rows:
                if r["id"] not in existing_ids:
                    rows.append(r)

    results = []
    for row in rows:
        score = compute_final_score(
            similarity=row["similarity"],
            created_at=row["created_at"],
            access_count=row["access_count"],
            decay_rate=DECAY_RATE,
            temporal_weight=req.temporal_weight,
            salience_score=float(row.get("salience_score", 0.5) or 0.5),
        )
        entity_overlap = _entity_overlap_score(req.query, row["content"])
        score = (1 - req.entity_weight) * score + (req.entity_weight * entity_overlap)
        results.append(SearchResult(memory=_row_to_memory(row), similarity=row["similarity"], final_score=score))
    results.sort(key=lambda r: r.final_score, reverse=True)
    return results[: req.limit]


# ---------------------------------------------------------------------------
# Forensic Recall endpoint — explicit mode, Evidence Memory with provenance
# ---------------------------------------------------------------------------

@app.post("/api/v1/recall/forensic", response_model=ForensicRecallResponse)
async def forensic_recall(req: ForensicRecallRequest, _key: str = Depends(verify_api_key)):
    """Forensic Recall: returns Evidence Memory with replay-grade provenance.

    Supports task/session/gateway/agent/event-type/artifact filters, cursor-based
    pagination, ABAC role filtering, and per-record redaction when the caller lacks
    the required role or explicitly excludes content.
    """
    filters: list[str] = [
        "memory_kind = 'evidence'",
        "valid_to IS NULL",
    ]
    params: list[Any] = []
    idx = 1

    if req.task_id:
        filters.append(f"metadata->>'task_id' = ${idx}")
        params.append(req.task_id)
        idx += 1
    if req.session_id:
        filters.append(f"metadata->>'session_id' = ${idx}")
        params.append(req.session_id)
        idx += 1
    if req.gateway_id:
        filters.append(f"metadata->>'gateway_id' = ${idx}")
        params.append(req.gateway_id)
        idx += 1
    if req.agent_id:
        filters.append(f"agent_id = ${idx}")
        params.append(req.agent_id)
        idx += 1
    if req.event_type:
        filters.append(f"metadata->>'event_type' = ${idx}")
        params.append(req.event_type)
        idx += 1
    if req.time_window_start:
        filters.append(f"created_at >= ${idx}")
        params.append(req.time_window_start)
        idx += 1
    if req.time_window_end:
        filters.append(f"created_at <= ${idx}")
        params.append(req.time_window_end)
        idx += 1
    if req.artifact_ref:
        filters.append(f"metadata->'artifact_refs' @> ${idx}::jsonb")
        params.append(json.dumps([req.artifact_ref]))
        idx += 1

    # Cursor-based pagination: cursor encodes "created_at__id" boundary
    if req.cursor:
        try:
            cursor_ts_str, cursor_id = req.cursor.rsplit("__", 1)
            filters.append(
                f"(created_at > ${idx}::timestamptz OR "
                f"(created_at = ${idx}::timestamptz AND id::text > ${idx + 1}))"
            )
            params.append(cursor_ts_str)
            params.append(cursor_id)
            idx += 2
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="Invalid pagination cursor.")

    where = " AND ".join(filters)

    # Fetch limit+1 to detect whether another page exists
    fetch_limit = req.limit + 1
    async with _tenant_conn(_key) as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, content, memory_type, memory_kind, agent_id,
                   metadata, created_at, valid_to
            FROM memories
            WHERE {where}
            ORDER BY created_at ASC, id ASC
            LIMIT {fetch_limit}
            """,
            *params,
        )

    has_next = len(rows) > req.limit
    page_rows = list(rows[: req.limit])

    items = [_build_forensic_item(row, req.include_content, req.agent_roles) for row in page_rows]

    next_cursor: str | None = None
    if has_next and page_rows:
        last = page_rows[-1]
        next_cursor = f"{last['created_at'].isoformat()}__{last['id']}"

    return ForensicRecallResponse(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Reflection Review Queue endpoints (Issue #28)
# ---------------------------------------------------------------------------

def _row_to_proposal(row: Any) -> ReflectionProposal:
    """Convert a reflection_proposals DB row to a ReflectionProposal model."""
    risk_flags = row.get("risk_flags") or []
    if isinstance(risk_flags, str):
        risk_flags = json.loads(risk_flags)

    source_evidence_ids = row.get("source_evidence_ids") or []
    if isinstance(source_evidence_ids, str):
        source_evidence_ids = json.loads(source_evidence_ids)

    return ReflectionProposal(
        proposal_id=row["proposal_id"],
        tenant_id=str(row["tenant_id"]),
        status=row["status"],
        source=row["source"],
        summary=row["summary"],
        content=row["content"],
        confidence=float(row["confidence"]),
        risk_flags=list(risk_flags),
        source_task_id=row.get("source_task_id"),
        source_session_id=row.get("source_session_id"),
        source_evidence_ids=list(source_evidence_ids),
        expires_at=row["expires_at"],
        telegram_message_id=row.get("telegram_message_id"),
        memory_id=row.get("memory_id"),
        superseded_by=row.get("superseded_by"),
        agent_id=row.get("agent_id") or "unknown",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _write_accepted_learning_memory(
    conn: Any,
    proposal: Any,
    content: str,
    review_status: str,
    tenant_id: str,
    supersedes_id: Optional[UUID] = None,
) -> UUID:
    """Write a Learning Memory to the memories table for an accepted proposal.

    Returns the new memory's UUID. Sets review_status explicitly so the memory
    is immediately eligible for default (learning) recall.
    """
    meta: dict[str, Any] = {
        "source": "reflection",
        "proposal_id": str(proposal["proposal_id"]),
        "source_task_id": proposal.get("source_task_id"),
        "source_session_id": proposal.get("source_session_id"),
        "source_evidence_ids": list(proposal.get("source_evidence_ids") or []),
        "allowed_roles": ["*"],
    }
    if supersedes_id:
        meta["supersedes"] = str(supersedes_id)

    c_hash = content_hash(content)
    row = await conn.fetchrow(
        """
        INSERT INTO memories (
            content, memory_type, memory_kind, agent_id, metadata,
            confidence, content_hash, tenant_id, review_status
        )
        VALUES ($1, 'lesson', 'learning', $2, $3::jsonb, $4, $5, $6, $7)
        RETURNING id
        """,
        content,
        proposal.get("agent_id") or "reflection-worker",
        json.dumps(meta),
        float(proposal["confidence"]),
        c_hash,
        tenant_id,
        review_status,
    )
    new_id = row["id"]

    # If this supersedes an earlier memory, mark that memory invalid
    if supersedes_id:
        now = datetime.now(timezone.utc)
        await conn.execute(
            """
            UPDATE memories
            SET valid_to = COALESCE(valid_to, $2),
                metadata = COALESCE(metadata, '{}'::jsonb)
                    || jsonb_build_object('invalidated_by', $1::text, 'invalidated_at', $2::text),
                updated_at = NOW()
            WHERE id = $3
              AND (valid_to IS NULL OR valid_to > $2)
            """,
            str(new_id),
            now,
            supersedes_id,
        )

    return new_id


@app.post("/api/v1/reflections/proposals", response_model=ReflectionProposal)
async def create_reflection_proposal(
    req: ReflectionProposalCreate,
    _key: str = Depends(verify_api_key),
):
    """Create a reflection proposal entering the 6-hour review window.

    The proposal is stored as 'pending' until approved, denied, edited,
    or automatically integrated after the review window expires.
    """
    tenant_id = str(getattr(_key, "tenant_id", DEFAULT_TENANT_ID))
    now = datetime.now(timezone.utc)
    expires_at = compute_proposal_expiry(now)

    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO reflection_proposals (
                tenant_id, status, source, summary, content, confidence,
                risk_flags, source_task_id, source_session_id,
                source_evidence_ids, expires_at, agent_id
            )
            VALUES ($1, 'pending', $2, $3, $4, $5, $6::jsonb, $7, $8, $9::jsonb, $10, $11)
            RETURNING *
            """,
            tenant_id,
            req.source.value,
            req.summary,
            req.content,
            req.confidence,
            json.dumps(req.risk_flags),
            req.source_task_id,
            req.source_session_id,
            json.dumps(req.source_evidence_ids),
            expires_at,
            req.agent_id,
        )

    return _row_to_proposal(row)


@app.get("/api/v1/reflections/proposals", response_model=list[ReflectionProposal])
async def list_reflection_proposals(
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 20,
    _key: str = Depends(verify_api_key),
):
    """List reflection proposals for the current tenant.

    Defaults to pending proposals. Supports status and source filters.
    """
    tenant_id = str(getattr(_key, "tenant_id", DEFAULT_TENANT_ID))
    filters = ["tenant_id = $1"]
    params: list[Any] = [tenant_id]
    idx = 2

    if status:
        filters.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    else:
        filters.append("status = 'pending'")

    if source:
        filters.append(f"source = ${idx}")
        params.append(source)
        idx += 1

    where = " AND ".join(filters)
    async with _tenant_conn(_key) as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM reflection_proposals
            WHERE {where}
            ORDER BY expires_at ASC
            LIMIT {limit}
            """,
            *params,
        )

    return [_row_to_proposal(row) for row in rows]


@app.get("/api/v1/reflections/proposals/{proposal_id}", response_model=ReflectionProposal)
async def get_reflection_proposal(
    proposal_id: UUID,
    _key: str = Depends(verify_api_key),
):
    """Fetch a single reflection proposal by ID."""
    tenant_id = str(getattr(_key, "tenant_id", DEFAULT_TENANT_ID))
    async with _tenant_conn(_key) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM reflection_proposals WHERE proposal_id = $1 AND tenant_id = $2",
            proposal_id,
            tenant_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return _row_to_proposal(row)


@app.post(
    "/api/v1/reflections/proposals/{proposal_id}/action",
    response_model=ReflectionActionResponse,
)
async def take_reflection_action(
    proposal_id: UUID,
    req: ReflectionActionRequest,
    _key: str = Depends(verify_api_key),
):
    """Take action on a reflection proposal: approve, deny, edit, or inspect.

    approve  — accepts the proposal and writes the Learning Memory.
    deny     — rejects the proposal; no memory is written.
    edit     — accepts with modified content and writes the Learning Memory.
               If the proposal is already accepted/accepted_by_timeout,
               creates a superseding Learning Memory (late feedback) without
               mutating the original accepted record.
    inspect  — logs the review action; proposal status is unchanged.
               Returns the proposal with source evidence IDs for forensic recall.
    """
    tenant_id = str(getattr(_key, "tenant_id", DEFAULT_TENANT_ID))
    decision = req.decision

    async with _tenant_conn(_key) as conn:
        proposal_row = await conn.fetchrow(
            "SELECT * FROM reflection_proposals WHERE proposal_id = $1 AND tenant_id = $2",
            proposal_id,
            tenant_id,
        )
        if not proposal_row:
            raise HTTPException(status_code=404, detail="Proposal not found")

        current_status = proposal_row["status"]
        memory_id: Optional[UUID] = None
        supersedes_id: Optional[UUID] = None
        new_status = current_status

        if decision == ReflectionAction.inspect:
            # inspect: record the action, leave status unchanged
            await conn.execute(
                """
                INSERT INTO reflection_actions (proposal_id, actor, decision, notes)
                VALUES ($1, $2, 'inspect', $3)
                """,
                proposal_id,
                req.actor,
                req.notes,
            )
            return ReflectionActionResponse(
                proposal_id=proposal_id,
                decision="inspect",
                status=current_status,
                memory_id=proposal_row.get("memory_id"),
            )

        if decision == ReflectionAction.deny:
            if current_status not in ("pending",):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot deny a proposal with status '{current_status}'.",
                )
            new_status = "rejected"
            await conn.execute(
                "UPDATE reflection_proposals SET status = 'rejected', updated_at = NOW() "
                "WHERE proposal_id = $1",
                proposal_id,
            )
            await conn.execute(
                """
                INSERT INTO reflection_actions (proposal_id, actor, decision, notes)
                VALUES ($1, $2, 'deny', $3)
                """,
                proposal_id,
                req.actor,
                req.notes,
            )
            return ReflectionActionResponse(
                proposal_id=proposal_id,
                decision="deny",
                status=new_status,
            )

        if decision == ReflectionAction.approve:
            if current_status != "pending":
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot approve a proposal with status '{current_status}'.",
                )
            new_status = "accepted"
            memory_id = await _write_accepted_learning_memory(
                conn=conn,
                proposal=proposal_row,
                content=proposal_row["content"],
                review_status="accepted",
                tenant_id=tenant_id,
            )
            await conn.execute(
                "UPDATE reflection_proposals SET status = 'accepted', memory_id = $2, "
                "updated_at = NOW() WHERE proposal_id = $1",
                proposal_id,
                memory_id,
            )
            await conn.execute(
                """
                INSERT INTO reflection_actions (proposal_id, actor, decision, notes)
                VALUES ($1, $2, 'approve', $3)
                """,
                proposal_id,
                req.actor,
                req.notes,
            )
            return ReflectionActionResponse(
                proposal_id=proposal_id,
                decision="approve",
                status=new_status,
                memory_id=memory_id,
            )

        if decision == ReflectionAction.edit:
            edited = req.edited_content
            if not edited:
                raise HTTPException(
                    status_code=422,
                    detail="edited_content is required for the 'edit' action.",
                )

            if current_status == "pending":
                # Normal edit-and-accept path
                new_status = "accepted"
                memory_id = await _write_accepted_learning_memory(
                    conn=conn,
                    proposal=proposal_row,
                    content=edited,
                    review_status="accepted",
                    tenant_id=tenant_id,
                )
                await conn.execute(
                    "UPDATE reflection_proposals SET status = 'accepted', memory_id = $2, "
                    "updated_at = NOW() WHERE proposal_id = $1",
                    proposal_id,
                    memory_id,
                )
            elif current_status in ("accepted", "accepted_by_timeout"):
                # Late feedback: create superseding memory without mutating history
                original_memory_id: Optional[UUID] = proposal_row.get("memory_id")
                supersedes_id = original_memory_id
                new_status = "superseded"
                memory_id = await _write_accepted_learning_memory(
                    conn=conn,
                    proposal=proposal_row,
                    content=edited,
                    review_status="accepted",
                    tenant_id=tenant_id,
                    supersedes_id=original_memory_id,
                )
                await conn.execute(
                    "UPDATE reflection_proposals SET status = 'superseded', superseded_by = $2, "
                    "updated_at = NOW() WHERE proposal_id = $1",
                    proposal_id,
                    proposal_id,
                )
            else:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot edit a proposal with status '{current_status}'.",
                )

            await conn.execute(
                """
                INSERT INTO reflection_actions
                    (proposal_id, actor, decision, notes, edited_content)
                VALUES ($1, $2, 'edit', $3, $4)
                """,
                proposal_id,
                req.actor,
                req.notes,
                edited,
            )
            return ReflectionActionResponse(
                proposal_id=proposal_id,
                decision="edit",
                status=new_status,
                memory_id=memory_id,
                supersedes_id=supersedes_id,
            )

    # Should not reach here
    raise HTTPException(status_code=422, detail="Unrecognised action.")  # pragma: no cover


@app.post("/api/v1/reflections/process-timeouts")
async def process_reflection_timeouts(_key: str = Depends(verify_api_key)):
    """Process expired pending proposals and integrate them as accepted_by_timeout.

    For each proposal whose review window has closed and is still pending:
      1. Writes the Learning Memory with review_status='accepted_by_timeout'.
      2. Updates the proposal status to 'accepted_by_timeout'.
      3. Records a 'timeout_accept' action in the audit trail.

    Returns the count of proposals processed.
    """
    tenant_id = str(getattr(_key, "tenant_id", DEFAULT_TENANT_ID))
    now = datetime.now(timezone.utc)
    processed = 0

    async with _tenant_conn(_key) as conn:
        expired_rows = await conn.fetch(
            """
            SELECT * FROM reflection_proposals
            WHERE tenant_id = $1
              AND status = 'pending'
              AND expires_at <= $2
            ORDER BY expires_at ASC
            """,
            tenant_id,
            now,
        )

        for row in expired_rows:
            pid = row["proposal_id"]
            try:
                memory_id = await _write_accepted_learning_memory(
                    conn=conn,
                    proposal=row,
                    content=row["content"],
                    review_status="accepted_by_timeout",
                    tenant_id=tenant_id,
                )
                await conn.execute(
                    "UPDATE reflection_proposals "
                    "SET status = 'accepted_by_timeout', memory_id = $2, updated_at = NOW() "
                    "WHERE proposal_id = $1",
                    pid,
                    memory_id,
                )
                await conn.execute(
                    """
                    INSERT INTO reflection_actions
                        (proposal_id, actor, decision, notes)
                    VALUES ($1, 'system', 'timeout_accept', $2)
                    """,
                    pid,
                    f"Review window expired at {now.isoformat()}",
                )
                processed += 1
            except Exception as exc:
                logger.error("Failed to process timeout for proposal %s: %s", pid, exc)

    return {"processed": processed, "timestamp": now.isoformat()}


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
async def get_graph_neighbors(
    memory_id: UUID,
    graph_name: str = "memu_graph",
    _key: str = Depends(verify_api_key),
):
    """Traverse graph to find 1st degree neighbors of a memory.

    Args:
        memory_id: UUID of the memory vertex to traverse from.
        graph_name: Name of the AGE graph to query (default: memu_graph).
    """
    graph_name = _validate_graph_identifier(graph_name)
    id_literal = _cypher_string_literal(str(memory_id))
    async with pool.acquire() as conn:
        try:
            await conn.execute("SET search_path = ag_catalog, \"$user\", public")
            cypher_sql = f"""
            SELECT * FROM cypher('{graph_name}', $$ 
                MATCH (m:Memory {{id: {id_literal}}})-[r]-(neighbor:Memory)
                RETURN type(r) as rel_type, neighbor
            $$) AS (rel_type agtype, neighbor agtype);
            """
            rows = await conn.fetch(cypher_sql)
            return {"ok": True, "neighbors": [dict(r) for r in rows]}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/memu/graph/stats")
async def get_graph_stats(
    graph_name: str = "memu_graph",
    _key: str = Depends(verify_api_key),
):
    """Return vertex count, edge count, and top relationship types for the knowledge graph.

    Args:
        graph_name: Name of the AGE graph to inspect (default: memu_graph).
    """
    graph_name = _validate_graph_identifier(graph_name)
    async with pool.acquire() as conn:
        try:
            await conn.execute("SET search_path = ag_catalog, \"$user\", public")

            # vertex count
            v_rows = await conn.fetch(
                f"SELECT * FROM cypher('{graph_name}', $$ MATCH (n) RETURN count(n) AS cnt $$) AS (cnt agtype);"
            )
            vertex_count = int(v_rows[0]["cnt"]) if v_rows else 0

            # edge count
            e_rows = await conn.fetch(
                f"SELECT * FROM cypher('{graph_name}', $$ MATCH ()-[r]->() RETURN count(r) AS cnt $$) AS (cnt agtype);"
            )
            edge_count = int(e_rows[0]["cnt"]) if e_rows else 0

            # top relation types (up to 10)
            rt_rows = await conn.fetch(
                f"""
                SELECT * FROM cypher('{graph_name}', $$
                    MATCH ()-[r]->()
                    RETURN type(r) AS rel_type, count(r) AS cnt
                    ORDER BY cnt DESC LIMIT 10
                $$) AS (rel_type agtype, cnt agtype);
                """
            )
            rel_types = [
                {"rel_type": str(r["rel_type"]).strip('"'), "count": int(r["cnt"])}
                for r in rt_rows
            ]

            return {
                "ok": True,
                "graph_name": graph_name,
                "vertex_count": vertex_count,
                "edge_count": edge_count,
                "top_rel_types": rel_types,
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/v1/memu/graph/path/{from_id}/{to_id}")
async def get_graph_path(
    from_id: UUID,
    to_id: UUID,
    graph_name: str = "memu_graph",
    max_hops: int = 5,
    _key: str = Depends(verify_api_key),
):
    """Find the shortest path between two memory vertices in the knowledge graph.

    Args:
        from_id: UUID of the source memory vertex.
        to_id: UUID of the target memory vertex.
        graph_name: Name of the AGE graph to query (default: memu_graph).
        max_hops: Maximum path length to search (default: 5, max: 10).
    """
    if max_hops < 1 or max_hops > 10:
        raise HTTPException(status_code=422, detail="max_hops must be between 1 and 10")
    graph_name = _validate_graph_identifier(graph_name)
    from_literal = _cypher_string_literal(str(from_id))
    to_literal = _cypher_string_literal(str(to_id))
    async with pool.acquire() as conn:
        try:
            await conn.execute("SET search_path = ag_catalog, \"$user\", public")
            cypher_sql = f"""
            SELECT * FROM cypher('{graph_name}', $$ 
                MATCH p = shortestPath(
                    (src:Memory {{id: {from_literal}}})-[*1..{max_hops}]-(dst:Memory {{id: {to_literal}}})
                )
                RETURN length(p) AS hops, [n in nodes(p) | n.id] AS node_ids,
                       [r in relationships(p) | type(r)] AS rel_types
            $$) AS (hops agtype, node_ids agtype, rel_types agtype);
            """
            rows = await conn.fetch(cypher_sql)
            if not rows:
                return {"ok": True, "found": False, "path": None}
            row = dict(rows[0])
            return {
                "ok": True,
                "found": True,
                "path": {
                    "hops": row["hops"],
                    "node_ids": row["node_ids"],
                    "rel_types": row["rel_types"],
                },
            }
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



# --- Entity Invalidation (Superseding) ---
# Matches Zep's entity supersession capability for temporal knowledge graphs

class SupersedeRequest(BaseModel):
    """Request to supersede an entity (mark as invalidated by a newer version)."""
    superseded_memory_id: UUID  # The old memory being superseded
    superseding_memory_id: UUID  # The new memory that supersedes it
    relationship: str = "supersedes"  # Default relationship type
    strength: float = 1.0  # Superseding relationships are strong by default
    metadata: Optional[dict[str, Any]] = None

class SupersedeResponse(BaseModel):
    """Response from a supersession operation."""
    ok: bool
    superseded_memory_id: UUID
    superseding_memory_id: UUID
    link_id: UUID
    message: str

@app.post("/api/v1/memu/supersede", response_model=SupersedeResponse)
async def supersede_entity(req: SupersedeRequest, _key: str = Depends(verify_api_key)):
    """
    Supersede an entity (like Zep's entity invalidation).
    
    This does two things:
    1. Sets valid_to on the superseded memory to NOW() (marks it as historical)
    2. Creates a 'supersedes' link between the new and old memory
    
    The superseded memory remains in the database but is excluded from
    temporal queries that filter for valid_to IS NULL.
    """
    async with _tenant_conn(_key) as conn:
        async with conn.transaction():
            # Step 1: Mark the old memory as superseded (set valid_to)
            old_memory = await conn.fetchrow(
                """
                UPDATE memories
                SET valid_to = NOW()
                WHERE id = $1 AND valid_to IS NULL
                RETURNING id, content, valid_to
                """,
                req.superseded_memory_id,
            )
            if not old_memory:
                raise HTTPException(
                    status_code=404,
                    detail=f"Memory {req.superseded_memory_id} not found or already superseded"
                )

            # Step 2: Verify the superseding memory exists and is current
            new_memory = await conn.fetchrow(
                """
                SELECT id, content FROM memories
                WHERE id = $1 AND valid_to IS NULL
                """,
                req.superseding_memory_id,
            )
            if not new_memory:
                raise HTTPException(
                    status_code=404,
                    detail=f"Superseding memory {req.superseding_memory_id} not found or already superseded"
                )

            # Step 3: Create the supersedes link
            link = await conn.fetchrow(
                """
                INSERT INTO memory_links (source_id, target_id, relationship, strength, metadata)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (source_id, target_id, relationship) DO NOTHING
                RETURNING id
                """,
                req.superseding_memory_id,  # source (the new one)
                req.superseded_memory_id,    # target (the old one)
                req.relationship,
                req.strength,
                req.metadata or {},
            )

            # If link already existed, fetch it
            if not link:
                link = await conn.fetchrow(
                    "SELECT id FROM memory_links WHERE source_id = $1 AND target_id = $2",
                    req.superseding_memory_id, req.superseded_memory_id
                )

    return SupersedeResponse(
        ok=True,
        superseded_memory_id=req.superseded_memory_id,
        superseding_memory_id=req.superseding_memory_id,
        link_id=link["id"],
        message=f"Memory {req.superseded_memory_id} superseded by {req.superseding_memory_id}"
    )

@app.get("/api/v1/memu/superseded/{memory_id}")
async def get_superseded_history(memory_id: UUID, _key: str = Depends(verify_api_key)):
    """
    Get the history of a memory that has been superseded.
    Returns the chain of supersession if this memory was superseded by others.
    """
    async with _tenant_conn(_key) as conn:
        # Find all memories that this memory supersedes (it is the source)
        supersedes = await conn.fetch(
            """
            SELECT ml.id as link_id, ml.created_at as superseded_at,
                   m.id, m.content, m.memory_type, m.agent_id, m.created_at
            FROM memory_links ml
            JOIN memories m ON m.id = ml.target_id
            WHERE ml.source_id = $1 AND ml.relationship = 'supersedes'
            ORDER BY ml.created_at DESC
            """,
            memory_id,
        )

        # Find what this memory was superseded by (it is the target)
        superseded_by = await conn.fetchrow(
            """
            SELECT ml.id as link_id, ml.source_id, ml.created_at as superseded_at,
                   m.content, m.memory_type, m.agent_id
            FROM memory_links ml
            JOIN memories m ON m.id = ml.source_id
            WHERE ml.target_id = $1 AND ml.relationship = 'supersedes'
            ORDER BY ml.created_at DESC
            LIMIT 1
            """,
            memory_id,
        )

    return {
        "ok": True,
        "memory_id": memory_id,
        "superseded_by": dict(superseded_by) if superseded_by else None,
        "supersedes": [dict(s) for s in supersedes],
        "supersedes_count": len(supersedes),
    }

@app.get("/api/v1/memu/entities/current")
async def get_current_entities(
    agent_id: str = None,
    memory_type: str = None,
    limit: int = 100,
    _key: str = Depends(verify_api_key),
):
    """
    Get current (non-superseded) entities, similar to Zep's entity retrieval.
    Only returns memories where valid_to IS NULL.
    """
    async with _tenant_conn(_key) as conn:
        filters = ["valid_to IS NULL"]
        params: list[Any] = []
        idx = 1

        if agent_id:
            filters.append(f"agent_id = ${idx}")
            params.append(agent_id)
            idx += 1
        if memory_type:
            filters.append(f"memory_type = ${idx}")
            params.append(memory_type)
            idx += 1

        where = " AND ".join(filters)
        params.append(limit)

        rows = await conn.fetch(
            f"""
            SELECT id, content, memory_type, agent_id, created_at, updated_at, metadata
            FROM memories
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx}
            """,
            *params,
        )

    return {
        "ok": True,
        "entities": [dict(r) for r in rows],
        "count": len(rows),
    }

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

class MemURecallCompatRequest(BaseModel):
    query: str
    limit: int = 5
    agent: str | None = None
    agent_id: str | None = None


@app.get("/api/v1/memu/search/recall")
async def recall_search_compat(
    q: str,
    limit: int = 5,
    agent: str | None = None,
    agent_id: str | None = None,
    _key: str = Depends(verify_api_key),
):
    return await recall_search(query=q, limit=limit, agent_id=agent_id or agent, _key=_key)


@app.post("/api/v1/memu/search/recall")
async def recall_search_post_compat(req: MemURecallCompatRequest, _key: str = Depends(verify_api_key)):
    return await recall_search(query=req.query, limit=req.limit, agent_id=req.agent_id or req.agent, _key=_key)


@app.post("/search/recall")
async def recall_search_post(req: MemURecallCompatRequest, _key: str = Depends(verify_api_key)):
    return await recall_search(query=req.query, limit=req.limit, agent_id=req.agent_id or req.agent, _key=_key)


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
        if embedding is None:
            rows = await conn.fetch(
                """
                SELECT query, agent_id, created_at, results_count, 0.0::float8 as similarity
                FROM search_history
                WHERE query ILIKE $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                f"%{normalized_query}%",
                limit,
            )
        else:
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

# ===========================================================================
# Memory Agent API endpoints (migration 014)
# ===========================================================================

from memu.concept_search import (
    fts_search,
    trgm_search,
    concept_lookup,
    concept_search_memories,
    hybrid_search as _hybrid_search_core,
    rrf_fuse,
)


class HybridSearchRequest(BaseModel):
    query: str
    limit: int = 10
    agent_id: str | None = None
    use_fts: bool = True
    use_concepts: bool = True
    temporal_weight: float = 0.3
    rrf_k: int = 60


class CompactionResult(BaseModel):
    id: str
    summary: str
    agent_id: str | None
    compaction_level: int
    concept_tags: list[str]
    source_count: int
    created_at: datetime


class AgentRunRequest(BaseModel):
    run_type: str = "full"    # 'compact', 'index', 'distill', 'full'
    model: str | None = None


@app.get("/compactions", summary="List memory compaction nodes")
async def list_compactions(
    agent_id: str | None = None,
    level: int | None = None,
    limit: int = 50,
    _key: str = Depends(verify_api_key),
):
    """List all hierarchical compaction/summary nodes. Non-destructive — originals unchanged."""
    filters = []
    params: list[Any] = []
    idx = 1
    if agent_id:
        filters.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1
    if level is not None:
        filters.append(f"compaction_level = ${idx}")
        params.append(level)
        idx += 1
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    try:
        async with _tenant_conn(_key) as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, summary, agent_id, compaction_level, concept_tags,
                       array_length(source_memory_ids, 1) AS source_count, created_at
                FROM memory_compactions
                {where}
                ORDER BY created_at DESC
                LIMIT {limit}
                """,
                *params,
            )
        return [
            {
                "id": str(r["id"]),
                "summary": r["summary"],
                "agent_id": r["agent_id"],
                "compaction_level": r["compaction_level"],
                "concept_tags": list(r["concept_tags"] or []),
                "source_count": r["source_count"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Compactions query failed: {e}")


@app.get("/concepts", summary="List top concepts from concept index")
async def list_concepts(limit: int = 50, _key: str = Depends(verify_api_key)):
    """List most frequent concepts extracted from memory corpus."""
    try:
        async with _tenant_conn(_key) as conn:
            rows = await conn.fetch(
                """
                SELECT concept, frequency, array_length(memory_ids, 1) AS memory_count, last_seen
                FROM memory_concept_index
                ORDER BY frequency DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            {
                "concept": r["concept"],
                "frequency": r["frequency"],
                "memory_count": r["memory_count"] or 0,
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Concepts query failed: {e}")


@app.get("/concepts/search", summary="Fast concept-based grep (no embedding needed)")
async def search_concepts(
    q: str,
    agent_id: str | None = None,
    limit: int = 20,
    _key: str = Depends(verify_api_key),
):
    """
    O(1) concept-based memory lookup — find memories by keyword/concept without embedding.
    Backed by pg_trgm index for fuzzy matching.
    """
    try:
        async with _tenant_conn(_key) as conn:
            concept_entries = await concept_lookup(conn, q, limit=10)
            memories = await concept_search_memories(conn, q, limit=limit, agent_id=agent_id)
        return {
            "query": q,
            "matched_concepts": [c["concept"] for c in concept_entries],
            "memories": [
                {
                    "id": str(m["id"]),
                    "content": m["content"],
                    "agent_id": m.get("agent_id"),
                    "memory_type": m.get("memory_type"),
                    "score": m.get("concept_score", 0.9),
                }
                for m in memories
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Concept search failed: {e}")


@app.post("/memories/hybrid-search", summary="RRF fusion: vector + FTS + concept results")
async def hybrid_search_endpoint(req: HybridSearchRequest, _key: str = Depends(verify_api_key)):
    """
    Hybrid retrieval using Reciprocal Rank Fusion over three channels:
    1. Vector similarity (existing /search pipeline)
    2. Full-text search (tsvector)
    3. Concept index lookup

    Returns a single fused, re-ranked result list.
    """
    # Step 1: vector results
    embedding = await get_embedding(req.query)
    vector_results = []
    if embedding:
        filters = []
        params: list[Any] = [str(embedding)]
        idx = 2
        if req.agent_id:
            filters.append(f"agent_id = ${idx}")
            params.append(req.agent_id)
            idx += 1
        where = (" AND " + " AND ".join(filters)) if filters else ""
        try:
            async with _tenant_conn(_key) as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT id, content, agent_id, memory_type, metadata,
                           1 - (embedding <=> $1::vector({EMBEDDING_DIMS})) AS similarity,
                           created_at, access_count, confidence, decay_score
                    FROM memories
                    WHERE embedding IS NOT NULL AND valid_to IS NULL{where}
                    ORDER BY embedding <=> $1::vector({EMBEDDING_DIMS})
                    LIMIT {req.limit * 2}
                    """,
                    *params,
                )
                vector_results = [dict(r) for r in rows]
        except Exception as e:
            logger.warning("Hybrid vector search failed: %s", e)

    # Step 2 & 3: FTS + concept via RRF
    async with _tenant_conn(_key) as conn:
        fused = await _hybrid_search_core(
            conn,
            req.query,
            vector_results=vector_results,
            limit=req.limit,
            agent_id=req.agent_id,
            use_fts=req.use_fts,
            use_concepts=req.use_concepts,
            rrf_k=req.rrf_k,
        )

    return [
        {
            "id": str(m["id"]),
            "content": m["content"],
            "agent_id": m.get("agent_id"),
            "memory_type": m.get("memory_type"),
            "rrf_score": m.get("rrf_score", 0.0),
            "similarity": m.get("similarity", 0.0),
        }
        for m in fused[:req.limit]
    ]


@app.post("/agent/run", summary="Trigger a memory agent run")
async def trigger_agent_run(req: AgentRunRequest, _key: str = Depends(verify_api_key)):
    """
    Trigger a memory agent run asynchronously.
    run_type: 'compact' | 'index' | 'distill' | 'full'
    """
    import subprocess, sys

    cmd = [sys.executable, "-m", "memu.memory_agent", f"--{req.run_type}"]
    if req.model:
        cmd += ["--model", req.model]

    try:
        # Fire and forget — don't wait for completion
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(os.path.dirname(__file__)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"status": "started", "run_type": req.run_type, "pid": proc.pid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start agent: {e}")


@app.get("/agent/runs", summary="List recent memory agent runs")
async def list_agent_runs(limit: int = 20, _key: str = Depends(verify_api_key)):
    """Show recent memory agent run history with stats."""
    try:
        async with _tenant_conn(_key) as conn:
            rows = await conn.fetch(
                """
                SELECT id, run_type, status, memories_processed, compactions_created,
                       concepts_indexed, model_used, started_at, finished_at, error
                FROM memory_agent_runs
                ORDER BY started_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            {
                "id": str(r["id"]),
                "run_type": r["run_type"],
                "status": r["status"],
                "memories_processed": r["memories_processed"],
                "compactions_created": r["compactions_created"],
                "concepts_indexed": r["concepts_indexed"],
                "model_used": r["model_used"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "error": r["error"],
            }
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent runs query failed: {e}")


# ---------------------------------------------------------------------------
# Core Memory API (NATS KV — Letta-parity)
# ---------------------------------------------------------------------------


class CoreMemoryBlockUpdate(BaseModel):
    content: str


class CoreMemoryAppendRequest(BaseModel):
    text: str


class CoreMemoryReplaceRequest(BaseModel):
    old_text: str
    new_text: str


class PageArchivalRequest(BaseModel):
    query: str
    limit: int = 3


def _require_core_memory() -> CoreMemoryManager:
    if _core_memory_mgr is None:
        raise HTTPException(status_code=503, detail="Core Memory Manager not available (NATS KV)")
    return _core_memory_mgr


@app.get("/api/v1/memu/core-memory/{agent_id}")
async def get_core_memory(agent_id: str, _key: str = Depends(verify_api_key)):
    """Get all core memory blocks for an agent."""
    mgr = _require_core_memory()
    mem = await mgr.get_agent_memory(agent_id)
    return mem.to_dict()


@app.get("/api/v1/memu/core-memory/{agent_id}/{block_name}")
async def get_core_memory_block(agent_id: str, block_name: str, _key: str = Depends(verify_api_key)):
    """Get a specific core memory block."""
    mgr = _require_core_memory()
    try:
        block = Block(block_name)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid block: {block_name}. Valid: {[b.value for b in Block]}")
    entry = await mgr.get_block(agent_id, block)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Block {block_name} not found for agent {agent_id}")
    return entry.to_dict()


@app.put("/api/v1/memu/core-memory/{agent_id}/{block_name}")
async def update_core_memory_block(
    agent_id: str, block_name: str, req: CoreMemoryBlockUpdate,
    revision: int = 0, _key: str = Depends(verify_api_key),
):
    """Update a core memory block (CAS-protected). Agent can only write Working_Context."""
    mgr = _require_core_memory()
    try:
        block = Block(block_name)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid block: {block_name}")
    try:
        entry = await mgr.update_block(agent_id, block, req.content, revision, caller="agent")
        return entry.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except CASConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/memu/core-memory/{agent_id}/{block_name}/append")
async def append_core_memory_block(
    agent_id: str, block_name: str, req: CoreMemoryAppendRequest,
    _key: str = Depends(verify_api_key),
):
    """Append text to a core memory block."""
    mgr = _require_core_memory()
    try:
        block = Block(block_name)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid block: {block_name}")
    try:
        entry = await mgr.append_block(agent_id, block, req.text, caller="agent")
        return entry.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except CASConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/memu/core-memory/{agent_id}/{block_name}/replace")
async def replace_core_memory_block(
    agent_id: str, block_name: str, req: CoreMemoryReplaceRequest,
    _key: str = Depends(verify_api_key),
):
    """Replace text within a core memory block."""
    mgr = _require_core_memory()
    try:
        block = Block(block_name)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid block: {block_name}")
    try:
        entry = await mgr.replace_in_block(agent_id, block, req.old_text, req.new_text, caller="agent")
        return entry.to_dict()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except CASConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/v1/memu/core-memory/{agent_id}/page-archival")
async def page_archival_endpoint(
    agent_id: str, req: PageArchivalRequest,
    _key: str = Depends(verify_api_key),
):
    """Search PostgreSQL archival memory and page results into Working_Context."""
    mgr = _require_core_memory()
    from memu.archival_pager import page_from_archival
    paged = await page_from_archival(
        mgr, pool, agent_id, req.query,
        get_embedding=get_embedding,
        limit=req.limit,
    )
    return {"paged": paged, "count": len(paged)}


@app.post("/api/v1/memu/core-memory/{agent_id}/bootstrap")
async def bootstrap_agent_core_memory(
    agent_id: str,
    system: str = "",
    persona: str = "",
    _key: str = Depends(verify_api_key),
):
    """Initialize all core memory blocks for an agent (idempotent)."""
    mgr = _require_core_memory()
    mem = await mgr.bootstrap_agent(agent_id, system=system, persona=persona)
    return mem.to_dict()


@app.get("/api/v1/memu/core-memory/{agent_id}/system-prompt")
async def get_system_prompt_fragment(agent_id: str, _key: str = Depends(verify_api_key)):
    """Render core memory blocks into a system prompt fragment for LLM injection."""
    mgr = _require_core_memory()
    mem = await mgr.get_agent_memory(agent_id)
    return {"agent_id": agent_id, "fragment": mem.as_system_prompt_fragment(), "total_tokens": mem.total_tokens}



# ---------------------------------------------------------------------------
# Prospective Memory API (Phase 4)
# ---------------------------------------------------------------------------


class ProspectiveMemoryRequest(BaseModel):
    intent: str
    trigger_condition: dict  # {"type": "time", "delay_seconds": 3600} or {"type": "event", "pattern": "..."}


@app.post("/api/v1/memu/memory/prospective")
async def create_prospective_memory(
    req: ProspectiveMemoryRequest,
    agent_id: str,
    _key: str = Depends(verify_api_key),
):
    """Create a prospective memory — a future-intent reminder.

    Starts a sleeping Temporal workflow that wakes on the trigger condition
    and forcefully injects the intent into the agent's Working_Context.
    """
    trigger = req.trigger_condition
    trigger_type = trigger.get("type", "time")

    if trigger_type == "time":
        delay_seconds = int(trigger.get("delay_seconds", 3600))
    else:
        # Event-based triggers default to 1 hour polling
        delay_seconds = int(trigger.get("delay_seconds", 3600))

    # Try to start Temporal workflow
    try:
        from temporalio.client import Client as TemporalClient
        temporal_url = os.environ.get("TEMPORAL_URL", "localhost:7233")
        client = await TemporalClient.connect(temporal_url)
        handle = await client.start_workflow(
            "ProspectiveMemoryWorkflow",
            args=[agent_id, req.intent, delay_seconds],
            id=f"prospective-{agent_id}-{hash(req.intent) % 10000}",
            task_queue="memu-workers",
        )
        return {
            "status": "scheduled",
            "workflow_id": handle.id,
            "agent_id": agent_id,
            "intent": req.intent,
            "delay_seconds": delay_seconds,
        }
    except Exception as e:
        logger.warning("Temporal unavailable for prospective memory, using asyncio fallback: %s", e)

        # Fallback: asyncio.create_task with sleep
        async def _fallback_reminder():
            import asyncio
            await asyncio.sleep(delay_seconds)
            if _core_memory_mgr:
                try:
                    reminder = f"[URGENT REMINDER: {req.intent}]"
                    entry = await _core_memory_mgr.get_block(agent_id, Block.WORKING_CONTEXT)
                    if entry:
                        new_content = f"{reminder}\n{entry.content}"
                        await _core_memory_mgr.update_block(
                            agent_id, Block.WORKING_CONTEXT, new_content,
                            entry.revision, caller="agent",
                        )
                    logger.info("Prospective memory triggered for %s: %s", agent_id, req.intent)
                except Exception as exc:
                    logger.error("Prospective memory injection failed: %s", exc)

        import asyncio
        asyncio.create_task(_fallback_reminder())
        return {
            "status": "scheduled_fallback",
            "agent_id": agent_id,
            "intent": req.intent,
            "delay_seconds": delay_seconds,
        }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
