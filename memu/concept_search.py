"""
concept_search.py — Fast grep-like concept retrieval and RRF fusion for memU.

Implements:
  - Reciprocal Rank Fusion (RRF) for blending vector + FTS + concept results
  - Concept-based O(1) lookup from memory_concept_index
  - pg_trgm-powered substring/fuzzy matching
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def rrf_fuse(result_sets: list[list[dict]], k: int = 60) -> list[dict]:
    """
    Fuse multiple ranked result lists using Reciprocal Rank Fusion.

    Each item in a result set must have an 'id' key.
    Returns unified list sorted by RRF score descending.

    Formula: score(d) = sum_r( 1 / (k + rank_r(d)) )
    k=60 is standard; higher k = less penalty for low ranks.
    """
    scores: dict[str, float] = {}
    items: dict[str, dict] = {}

    for result_set in result_sets:
        for rank, item in enumerate(result_set, start=1):
            item_id = str(item.get("id", ""))
            if not item_id:
                continue
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
            if item_id not in items:
                items[item_id] = item

    fused = sorted(items.values(), key=lambda x: scores.get(str(x.get("id", "")), 0.0), reverse=True)
    for item in fused:
        item["rrf_score"] = scores.get(str(item.get("id", "")), 0.0)
    return fused


# ---------------------------------------------------------------------------
# FTS search (tsvector)
# ---------------------------------------------------------------------------

async def fts_search(
    conn: asyncpg.Connection,
    query: str,
    limit: int = 20,
    agent_id: str | None = None,
    table: str = "memories",
    content_col: str = "content",
) -> list[dict]:
    """Full-text search using pg tsvector + ts_rank. Returns dicts with id, content, fts_rank."""
    filters = ["fts_vector @@ plainto_tsquery('english', $1)"]
    params: list[Any] = [query]
    idx = 2

    if agent_id:
        filters.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1

    where = " AND ".join(filters)
    try:
        rows = await conn.fetch(
            f"""
            SELECT id, {content_col} AS content, agent_id, memory_type, metadata,
                   ts_rank(fts_vector, plainto_tsquery('english', $1)) AS fts_rank,
                   created_at, access_count, confidence, decay_score
            FROM {table}
            WHERE {where}
            ORDER BY fts_rank DESC
            LIMIT {limit}
            """,
            *params,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("FTS search failed (may need migration 014): %s", e)
        return []


async def trgm_search(
    conn: asyncpg.Connection,
    query: str,
    limit: int = 20,
    agent_id: str | None = None,
    table: str = "memories",
    content_col: str = "content",
) -> list[dict]:
    """Trigram similarity search — great for partial/fuzzy keyword matching."""
    filters = ["content % $1 OR content ILIKE $2"]
    params: list[Any] = [query, f"%{query}%"]
    idx = 3

    if agent_id:
        filters.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1

    where = " AND ".join(filters)
    try:
        rows = await conn.fetch(
            f"""
            SELECT id, {content_col} AS content, agent_id, memory_type, metadata,
                   similarity(content, $1) AS trgm_score,
                   created_at, access_count, confidence, decay_score
            FROM {table}
            WHERE {where}
            ORDER BY trgm_score DESC
            LIMIT {limit}
            """,
            *params,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Trigram search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Concept index lookup
# ---------------------------------------------------------------------------

async def concept_lookup(
    conn: asyncpg.Connection,
    query: str,
    limit: int = 10,
    fuzzy: bool = True,
) -> list[dict]:
    """
    Fast concept-based memory lookup — no embedding needed.
    Finds concept entries matching query, returns memory IDs + frequency info.
    """
    try:
        if fuzzy:
            rows = await conn.fetch(
                """
                SELECT concept, memory_ids, compaction_ids, frequency, last_seen
                FROM memory_concept_index
                WHERE concept ILIKE $1 OR concept % $2
                ORDER BY frequency DESC
                LIMIT $3
                """,
                f"%{query}%",
                query,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT concept, memory_ids, compaction_ids, frequency, last_seen
                FROM memory_concept_index
                WHERE concept = $1
                LIMIT $2
                """,
                query.lower().strip(),
                limit,
            )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Concept lookup failed (may need migration 014): %s", e)
        return []


async def concept_search_memories(
    conn: asyncpg.Connection,
    query: str,
    limit: int = 20,
    agent_id: str | None = None,
) -> list[dict]:
    """
    Retrieve actual memory records referenced by concept index.
    Combines concept lookup → memory fetch → return enriched results.
    """
    concepts = await concept_lookup(conn, query, limit=5)
    if not concepts:
        return []

    # Collect all memory IDs from matching concepts
    all_ids: list[str] = []
    for c in concepts:
        all_ids.extend([str(mid) for mid in (c.get("memory_ids") or [])])

    if not all_ids:
        return []

    # Deduplicate preserving order
    seen: set[str] = set()
    unique_ids: list[str] = []
    for mid in all_ids:
        if mid not in seen:
            seen.add(mid)
            unique_ids.append(mid)

    unique_ids = unique_ids[:limit]

    filters = ["id = ANY($1::uuid[])"]
    params: list[Any] = [unique_ids]
    if agent_id:
        filters.append("agent_id = $2")
        params.append(agent_id)

    where = " AND ".join(filters)
    rows = await conn.fetch(
        f"""
        SELECT id, content, agent_id, memory_type, metadata,
               0.9::float8 AS concept_score,
               created_at, access_count, confidence, decay_score
        FROM memories
        WHERE {where}
        ORDER BY array_position($1::uuid[], id)
        LIMIT {limit}
        """,
        *params,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Hybrid search — RRF fusion of vector + FTS + concept
# ---------------------------------------------------------------------------

async def hybrid_search(
    conn: asyncpg.Connection,
    query: str,
    vector_results: list[dict],
    limit: int = 10,
    agent_id: str | None = None,
    use_fts: bool = True,
    use_concepts: bool = True,
    rrf_k: int = 60,
) -> list[dict]:
    """
    Hybrid search: fuse vector results with FTS and concept index results via RRF.

    vector_results: already-computed vector search results (list of dicts with 'id')
    Returns RRF-fused and re-ranked results.
    """
    result_sets: list[list[dict]] = [vector_results]

    if use_fts:
        fts_res = await fts_search(conn, query, limit=limit * 2, agent_id=agent_id)
        if fts_res:
            result_sets.append(fts_res)

    if use_concepts:
        concept_res = await concept_search_memories(conn, query, limit=limit * 2, agent_id=agent_id)
        if concept_res:
            result_sets.append(concept_res)

    if len(result_sets) == 1:
        # Only vector results — return as-is
        return vector_results[:limit]

    fused = rrf_fuse(result_sets, k=rrf_k)
    return fused[:limit]
