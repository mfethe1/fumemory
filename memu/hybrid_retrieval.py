"""
Hybrid retrieval engine for memU.

Implements:
- Reciprocal Rank Fusion (RRF) of semantic + BM25 results
- RAPTOR-style hierarchical retrieval (cluster → leaf)
- Keyword grep index for fast exact/prefix matches

Based on research: Zep temporal graphs, mem0 hybrid patterns, pgvector 0.8 ANN improvements.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any
from uuid import UUID


# --- Reciprocal Rank Fusion ---

def rrf_score(rank: int, k: int = 60) -> float:
    """Reciprocal Rank Fusion score. k=60 is standard."""
    return 1.0 / (k + rank)


def fuse_results(
    semantic_hits: list[dict],
    bm25_hits: list[dict],
    semantic_weight: float = 0.7,
    bm25_weight: float = 0.3,
) -> list[dict]:
    """
    Fuse semantic and BM25 result lists using Reciprocal Rank Fusion.

    Args:
        semantic_hits: List of dicts with 'id' key, ordered by semantic score.
        bm25_hits: List of dicts with 'id' key, ordered by BM25 rank.
        semantic_weight: Weight for semantic RRF scores.
        bm25_weight: Weight for BM25 RRF scores.

    Returns:
        Merged list ordered by fused RRF score, highest first.
    """
    scores: dict[Any, float] = {}
    hits_by_id: dict[Any, dict] = {}

    for rank, hit in enumerate(semantic_hits):
        hid = hit["id"]
        scores[hid] = scores.get(hid, 0) + semantic_weight * rrf_score(rank)
        hits_by_id[hid] = hit

    for rank, hit in enumerate(bm25_hits):
        hid = hit["id"]
        scores[hid] = scores.get(hid, 0) + bm25_weight * rrf_score(rank)
        if hid not in hits_by_id:
            hits_by_id[hid] = hit

    fused = [
        {**hits_by_id[hid], "rrf_score": score}
        for hid, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)
    ]
    return fused


# --- PostgreSQL query builders ---

SEMANTIC_SEARCH_SQL = """
    SELECT
        id, content, memory_type, agent_id, metadata,
        confidence, access_count, decay_score, created_at, updated_at,
        cluster_id,
        1 - (embedding <=> $1::vector) AS similarity
    FROM memories
    WHERE embedding IS NOT NULL
      {filters}
    ORDER BY embedding <=> $1::vector
    LIMIT $2
"""

BM25_SEARCH_SQL = """
    SELECT
        id, content, memory_type, agent_id, metadata,
        confidence, access_count, decay_score, created_at, updated_at,
        cluster_id,
        ts_rank_cd(content_tsv, plainto_tsquery('english', $1)) AS bm25_score
    FROM memories
    WHERE content_tsv @@ plainto_tsquery('english', $1)
      {filters}
    ORDER BY bm25_score DESC
    LIMIT $2
"""

CLUSTER_SEARCH_SQL = """
    SELECT
        id, summary AS content, level, child_ids, agent_scope,
        memory_count,
        1 - (embedding <=> $1::vector) AS similarity
    FROM memory_clusters
    WHERE level = $2
      AND embedding IS NOT NULL
      {scope_filter}
    ORDER BY embedding <=> $1::vector
    LIMIT $3
"""

CLUSTER_CHILDREN_SQL = """
    SELECT
        id, content, memory_type, agent_id, metadata,
        confidence, access_count, decay_score, created_at, updated_at,
        cluster_id
    FROM memories
    WHERE cluster_id = ANY($1::uuid[])
    ORDER BY decay_score DESC, confidence DESC
    LIMIT $2
"""


async def hybrid_search(
    conn,
    query: str,
    embedding: list[float],
    *,
    limit: int = 10,
    agent_id: str | None = None,
    memory_type: str | None = None,
    semantic_weight: float = 0.7,
    bm25_weight: float = 0.3,
) -> list[dict]:
    """
    Execute hybrid search: semantic + BM25 fused via RRF.

    Args:
        conn: asyncpg connection.
        query: Raw text query for BM25.
        embedding: Pre-computed query embedding for semantic search.
        limit: Final result count.
        agent_id: Optional agent filter.
        memory_type: Optional type filter.
        semantic_weight: RRF weight for semantic results (default 0.7).
        bm25_weight: RRF weight for BM25 results (default 0.3).

    Returns:
        Fused, ranked list of memory dicts.
    """
    filter_clauses = []
    filter_params: list[Any] = []
    param_offset = 2  # $1=embedding/$query, $2=limit already used

    if agent_id:
        param_offset += 1
        filter_clauses.append(f"agent_id = ${param_offset}")
        filter_params.append(agent_id)
    if memory_type:
        param_offset += 1
        filter_clauses.append(f"memory_type = ${param_offset}")
        filter_params.append(memory_type)

    filters_sql = ("AND " + " AND ".join(filter_clauses)) if filter_clauses else ""
    fetch_limit = limit * 3  # fetch more, fuse to top-limit

    # Run both searches concurrently
    sem_sql = SEMANTIC_SEARCH_SQL.format(filters=filters_sql)
    bm25_sql = BM25_SEARCH_SQL.format(filters=filters_sql)

    sem_task = conn.fetch(sem_sql, str(embedding), fetch_limit, *filter_params)
    bm25_task = conn.fetch(bm25_sql, query, fetch_limit, *filter_params)

    semantic_rows, bm25_rows = await asyncio.gather(sem_task, bm25_task)

    def row_to_dict(row) -> dict:
        return dict(row)

    fused = fuse_results(
        [row_to_dict(r) for r in semantic_rows],
        [row_to_dict(r) for r in bm25_rows],
        semantic_weight=semantic_weight,
        bm25_weight=bm25_weight,
    )
    return fused[:limit]


async def raptor_search(
    conn,
    query: str,
    embedding: list[float],
    *,
    limit: int = 10,
    agent_id: str | None = None,
    max_cluster_level: int = 2,
) -> list[dict]:
    """
    RAPTOR-style hierarchical retrieval.

    1. Search top-level cluster summaries (coarse).
    2. Drill into best-matching clusters to fetch leaf memories.
    3. Combine cluster context + leaf memories.

    This gives the model both a high-level map (cluster summaries)
    and relevant specifics (leaf memories), like a table of contents + chapters.

    Args:
        conn: asyncpg connection.
        query: Text query.
        embedding: Pre-computed query embedding.
        limit: Final leaf memory count.
        agent_id: Optional scope filter.
        max_cluster_level: Highest cluster level to start from (default 2 = supercluster).
    """
    results = []
    cluster_summaries = []

    scope_filter = "AND agent_scope = $4" if agent_id else ""

    # Start from highest available cluster level, drill down
    for level in range(max_cluster_level, -1, -1):
        if level == 0:
            # Bottom level — fetch leaf memories directly
            break
        cluster_rows = await conn.fetch(
            CLUSTER_SEARCH_SQL.format(scope_filter=scope_filter),
            str(embedding),
            level,
            min(5, limit),
            *([agent_id] if agent_id else []),
        )
        if cluster_rows:
            cluster_summaries.extend([dict(r) for r in cluster_rows[:3]])
            # Drill into top cluster's children
            top_cluster = cluster_rows[0]
            child_cluster_ids = top_cluster["child_ids"]
            if child_cluster_ids:
                # Fetch leaf memories from child clusters
                leaf_rows = await conn.fetch(
                    CLUSTER_CHILDREN_SQL,
                    child_cluster_ids,
                    limit,
                )
                results.extend([dict(r) for r in leaf_rows])
                if len(results) >= limit:
                    break

    # Deduplicate
    seen = set()
    unique_results = []
    for r in results:
        rid = str(r.get("id"))
        if rid not in seen:
            seen.add(rid)
            unique_results.append(r)

    return {
        "cluster_context": cluster_summaries,
        "memories": unique_results[:limit],
    }
