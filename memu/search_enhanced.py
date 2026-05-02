"""
Enhanced search endpoints — drop-in addition to api.py.

Adds:
  POST /search/hybrid     — RRF fusion of semantic + BM25
  POST /search/raptor     — Hierarchical cluster-aware retrieval
  GET  /search/grep?q=... — Fast keyword grep via keyword index

Wire into app with:
  from memu.search_enhanced import router as enhanced_router
  app.include_router(enhanced_router)
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

# These will be injected at import time from api.py's module-level objects
# when this router is registered on the app.
# To use standalone, set MEMU_POOL externally.
_pool = None  # set via set_pool()
_get_embedding_fn = None  # set via set_embedding_fn()
_verify_key_fn = None  # set via set_verify_key_fn()

router = APIRouter(tags=["enhanced-search"])

memu_key_header = APIKeyHeader(name="X-MemU-Key", auto_error=False)
legacy_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_api_key(
    memu_key: str | None = Security(memu_key_header),
    legacy_key: str | None = Security(legacy_api_key_header),
):
    if _verify_key_fn is None:
        raise HTTPException(503, "Auth verifier not configured")
    return await _verify_key_fn(memu_key=memu_key, legacy_key=legacy_key)


def configure(pool, get_embedding, verify_api_key):
    """Call this from api.py lifespan/startup to inject dependencies."""
    global _pool, _get_embedding_fn, _verify_key_fn
    _pool = pool
    _get_embedding_fn = get_embedding
    _verify_key_fn = verify_api_key


# --- Request/Response models ---

class HybridSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(10, ge=1, le=50)
    agent_id: str | None = None
    memory_type: str | None = None
    semantic_weight: float = Field(0.7, ge=0.0, le=1.0)
    bm25_weight: float = Field(0.3, ge=0.0, le=1.0)


class RaptorSearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(10, ge=1, le=50)
    agent_id: str | None = None
    include_cluster_context: bool = True


class GrepRequest(BaseModel):
    q: str = Field(..., min_length=1)
    limit: int = Field(20, ge=1, le=100)
    agent_id: str | None = None


# --- Helpers ---

def _row_to_dict(row) -> dict:
    result = dict(row)
    # Remove binary embedding from API response
    result.pop("embedding", None)
    return result


# --- Endpoints ---

@router.post("/search/hybrid")
@router.post("/search/recall")
@router.post("/api/v1/memu/search/recall")
async def hybrid_search_endpoint(
    req: HybridSearchRequest,
    _key: str = Depends(_require_api_key),
):
    """
    Hybrid semantic + BM25 search using Reciprocal Rank Fusion.
    Better recall than pure semantic for keyword-heavy queries.
    """
    if _pool is None:
        raise HTTPException(503, "Pool not configured")

    embedding = await _get_embedding_fn(req.query)

    # Build filter SQL snippets
    filter_clauses = []
    filter_params: list[Any] = []

    if req.agent_id:
        filter_params.append(req.agent_id)
        filter_clauses.append(f"agent_id = ${len(filter_params) + 2}")
    if req.memory_type:
        filter_params.append(req.memory_type)
        filter_clauses.append(f"memory_type = ${len(filter_params) + 2}")

    filters_sql = ("AND " + " AND ".join(filter_clauses)) if filter_clauses else ""
    fetch_limit = req.limit * 3

    async with _pool.acquire() as conn:
        sem_rows = await conn.fetch(
            f"""
            SELECT id, content, memory_type, agent_id, metadata, confidence,
                   access_count, decay_score, created_at, updated_at,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE embedding IS NOT NULL {filters_sql}
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            str(embedding), fetch_limit, *filter_params,
        )

        bm25_rows = await conn.fetch(
            f"""
            SELECT id, content, memory_type, agent_id, metadata, confidence,
                   access_count, decay_score, created_at, updated_at,
                   ts_rank_cd(content_tsv, plainto_tsquery('english', $1)) AS bm25_score
            FROM memories
            WHERE content_tsv @@ plainto_tsquery('english', $1) {filters_sql}
            ORDER BY bm25_score DESC
            LIMIT $2
            """,
            req.query, fetch_limit, *filter_params,
        )

    # RRF fusion
    k = 60
    scores: dict[Any, float] = {}
    all_rows: dict[Any, dict] = {}

    for rank, row in enumerate(sem_rows):
        rid = str(row["id"])
        scores[rid] = scores.get(rid, 0) + req.semantic_weight * (1.0 / (k + rank))
        all_rows[rid] = _row_to_dict(row)

    for rank, row in enumerate(bm25_rows):
        rid = str(row["id"])
        scores[rid] = scores.get(rid, 0) + req.bm25_weight * (1.0 / (k + rank))
        if rid not in all_rows:
            all_rows[rid] = _row_to_dict(row)

    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:req.limit]

    return {
        "ok": True,
        "query": req.query,
        "method": "hybrid-rrf",
        "results": [
            {**all_rows[rid], "rrf_score": score}
            for rid, score in fused
            if rid in all_rows
        ],
    }


@router.post("/search/raptor")
async def raptor_search_endpoint(
    req: RaptorSearchRequest,
    _key: str = Depends(_require_api_key),
):
    """
    RAPTOR hierarchical retrieval.
    Returns cluster summaries (high-level context) + leaf memories (specifics).
    Best for: broad questions, "what do we know about X?" style queries.
    """
    if _pool is None:
        raise HTTPException(503, "Pool not configured")

    embedding = await _get_embedding_fn(req.query)
    emb_str = str(embedding)

    async with _pool.acquire() as conn:
        # 1. Find best-matching clusters at level 1 (summaries of summaries)
        scope_filter = "AND agent_scope = $4" if req.agent_id else ""
        cluster_params = [emb_str, 1, min(5, req.limit)] + ([req.agent_id] if req.agent_id else [])

        cluster_rows = await conn.fetch(
            f"""
            SELECT id, summary, level, child_ids, agent_scope, memory_count,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM memory_clusters
            WHERE level = $2 AND embedding IS NOT NULL {scope_filter}
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            *cluster_params,
        )

        cluster_context = [dict(r) for r in cluster_rows]

        # 2. Get top-matching leaf memories via hybrid search
        leaf_rows = await conn.fetch(
            """
            SELECT id, content, memory_type, agent_id, metadata, confidence,
                   access_count, decay_score, created_at,
                   1 - (embedding <=> $1::vector) AS similarity
            FROM memories
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            emb_str, req.limit,
        )

    return {
        "ok": True,
        "query": req.query,
        "method": "raptor",
        "cluster_context": cluster_context if req.include_cluster_context else [],
        "memories": [_row_to_dict(r) for r in leaf_rows],
    }


@router.get("/search/grep")
async def grep_search_endpoint(
    q: str,
    limit: int = 20,
    agent_id: str | None = None,
    _key: str = Depends(_require_api_key),
):
    """
    Fast keyword grep using the keyword index + PostgreSQL full-text.
    Returns: exact and near-exact keyword matches, very fast.
    Use for: known entity names, project names, specific terms.
    """
    if _pool is None:
        raise HTTPException(503, "Pool not configured")

    async with _pool.acquire() as conn:
        # Try keyword index first (fastest)
        kw_rows = await conn.fetch(
            """
            SELECT m.id, m.content, m.memory_type, m.agent_id,
                   m.metadata, m.confidence, m.created_at,
                   ki.weight AS keyword_score
            FROM memory_keyword_index ki
            JOIN memories m ON m.id = ki.memory_id
            WHERE ki.keyword ILIKE $1
              AND ($2::text IS NULL OR m.agent_id = $2)
            ORDER BY ki.weight DESC
            LIMIT $3
            """,
            f"%{q}%", agent_id, limit,
        )

        if kw_rows:
            return {
                "ok": True,
                "query": q,
                "method": "keyword-index",
                "results": [_row_to_dict(r) for r in kw_rows],
            }

        # Fallback: PostgreSQL ILIKE scan
        ilike_rows = await conn.fetch(
            """
            SELECT id, content, memory_type, agent_id, metadata, confidence, created_at
            FROM memories
            WHERE content ILIKE $1
              AND ($2::text IS NULL OR agent_id = $2)
            ORDER BY created_at DESC
            LIMIT $3
            """,
            f"%{q}%", agent_id, limit,
        )

        return {
            "ok": True,
            "query": q,
            "method": "ilike-fallback",
            "results": [_row_to_dict(r) for r in ilike_rows],
        }
