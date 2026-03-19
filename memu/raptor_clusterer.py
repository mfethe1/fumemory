"""
RAPTOR Hierarchical Memory Clusterer.

Implements the RAPTOR (Recursive Abstractive Processing for Tree-Organized Retrieval)
pattern for memU:

1. Take all leaf memories (or unclustered ones)
2. Cluster by embedding similarity (k-means or DBSCAN)
3. For each cluster: call LLM to write a summary
4. Embed the summary → store as a memory_cluster row
5. Recurse: cluster the cluster-summaries into super-clusters
6. Result: a 2-3 level tree that allows coarse→fine retrieval

Reference: arXiv:2401.18059 "RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval"

This runs as the idle Memory Agent using Nemotron 3 Nano (local, $0 cost).
Falls back to qwen2.5:3b if Nemotron isn't ready.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any
from uuid import UUID
import httpx
import numpy as np

logger = logging.getLogger(__name__)

MEMU_API_URL = os.environ.get("MEMU_API_URL", "https://api-production-86f5.up.railway.app")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Prefer Nemotron 3 Nano for $0 cost, fall back to qwen2.5:3b
PREFERRED_SUMMARIZE_MODEL = "nemotron-3-nano:30b"
FALLBACK_SUMMARIZE_MODEL = "qwen2.5:3b"

CLUSTER_SIZE_MIN = 5      # Minimum memories to form a cluster
CLUSTER_SIZE_MAX = 50     # Split clusters larger than this
MAX_SUMMARY_TOKENS = 300  # Keep cluster summaries tight


def _get_summarize_model() -> str:
    """Check which local model is available."""
    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        if PREFERRED_SUMMARIZE_MODEL in models:
            return PREFERRED_SUMMARIZE_MODEL
    except Exception:
        pass
    return FALLBACK_SUMMARIZE_MODEL


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Fast cosine similarity."""
    a_arr = np.array(a)
    b_arr = np.array(b)
    denom = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    if denom == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / denom)


def kmeans_cluster(
    embeddings: list[list[float]],
    n_clusters: int,
    n_iterations: int = 20,
) -> list[int]:
    """
    Simple k-means clustering on embeddings.
    Returns cluster assignment for each embedding.
    """
    if len(embeddings) <= n_clusters:
        return list(range(len(embeddings)))

    arr = np.array(embeddings, dtype=np.float32)
    # Initialize centroids by random sampling
    rng = np.random.default_rng(42)
    indices = rng.choice(len(arr), n_clusters, replace=False)
    centroids = arr[indices].copy()

    labels = np.zeros(len(arr), dtype=int)

    for _ in range(n_iterations):
        # Assign each point to nearest centroid
        for i, vec in enumerate(arr):
            sims = [cosine_similarity(vec.tolist(), c.tolist()) for c in centroids]
            labels[i] = int(np.argmax(sims))

        # Recompute centroids
        new_centroids = np.zeros_like(centroids)
        for k in range(n_clusters):
            members = arr[labels == k]
            if len(members) > 0:
                new_centroids[k] = members.mean(axis=0)
            else:
                new_centroids[k] = centroids[k]

        if np.allclose(centroids, new_centroids, atol=1e-4):
            break
        centroids = new_centroids

    return labels.tolist()


async def _summarize_batch(texts: list[str], model: str) -> str:
    """Ask local LLM to summarize a batch of memory texts."""
    combined = "\n\n---\n\n".join(texts[:20])  # Cap to avoid context overflow
    prompt = (
        "You are a memory distillation assistant. "
        "Read these memory entries and write a concise 2-4 sentence summary "
        "capturing the key facts, decisions, and patterns. "
        "Be specific and dense — no filler.\n\n"
        f"MEMORIES:\n{combined}\n\nSUMMARY:"
    )

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": MAX_SUMMARY_TOKENS, "temperature": 0.1},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "").strip()


async def _get_embedding(text: str) -> list[float]:
    """Get embedding from local qwen3-embedding via Ollama."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": "qwen3-embedding:latest", "prompt": text},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


async def fetch_unclustered_memories(
    conn,
    agent_scope: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch memories without a cluster assignment."""
    filters = ["cluster_id IS NULL", "embedding IS NOT NULL"]
    params: list[Any] = []

    if agent_scope:
        params.append(agent_scope)
        filters.append(f"agent_id = ${len(params)}")

    params.append(limit)
    where = " AND ".join(filters)

    rows = await conn.fetch(
        f"""
        SELECT id, content, embedding, agent_id, memory_type, confidence
        FROM memories
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return [dict(r) for r in rows]


async def build_clusters(
    conn,
    memories: list[dict],
    level: int = 0,
    agent_scope: str | None = None,
    model: str | None = None,
) -> list[UUID]:
    """
    Cluster memories and create memory_cluster rows.

    Returns list of created cluster UUIDs.
    """
    if len(memories) < CLUSTER_SIZE_MIN:
        logger.info(f"Too few memories to cluster ({len(memories)}), skipping")
        return []

    if model is None:
        model = _get_summarize_model()
        logger.info(f"Using model: {model}")

    # Determine cluster count
    n_clusters = max(2, min(len(memories) // CLUSTER_SIZE_MIN, len(memories) // 10))

    # Extract embeddings
    embeddings = []
    for m in memories:
        emb = m.get("embedding")
        if emb is None:
            continue
        if isinstance(emb, str):
            # asyncpg returns vector as string "[...]"
            emb = json.loads(emb)
        embeddings.append(emb)

    if len(embeddings) < CLUSTER_SIZE_MIN:
        return []

    logger.info(f"Clustering {len(embeddings)} memories into {n_clusters} clusters (level {level})")
    labels = kmeans_cluster(embeddings, n_clusters)

    cluster_ids = []
    for cluster_idx in range(n_clusters):
        member_indices = [i for i, lbl in enumerate(labels) if lbl == cluster_idx]
        members = [memories[i] for i in member_indices if i < len(memories)]

        if len(members) < 2:
            continue

        texts = [m["content"][:800] for m in members]  # Truncate for summarization
        t0 = time.time()

        try:
            summary = await _summarize_batch(texts, model)
        except Exception as e:
            logger.warning(f"Summarization failed for cluster {cluster_idx}: {e}")
            # Fallback: first sentence of longest memory
            summary = max(texts, key=len)[:300]

        duration_ms = int((time.time() - t0) * 1000)

        # Embed the summary
        try:
            summary_embedding = await _get_embedding(summary)
        except Exception as e:
            logger.warning(f"Embedding failed for cluster {cluster_idx}: {e}")
            # Use centroid of member embeddings
            embs = [
                json.loads(m["embedding"]) if isinstance(m["embedding"], str) else m["embedding"]
                for m in members
                if m.get("embedding")
            ]
            summary_embedding = np.array(embs).mean(axis=0).tolist() if embs else None

        member_ids = [m["id"] for m in members]

        # Insert cluster row
        cluster_id = await conn.fetchval(
            """
            INSERT INTO memory_clusters
                (level, summary, embedding, child_ids, agent_scope, memory_count)
            VALUES ($1, $2, $3::vector, $4::uuid[], $5, $6)
            RETURNING id
            """,
            level,
            summary,
            str(summary_embedding) if summary_embedding else None,
            member_ids,
            agent_scope,
            len(members),
        )

        # Update memories with cluster assignment
        await conn.execute(
            "UPDATE memories SET cluster_id = $1 WHERE id = ANY($2::uuid[])",
            cluster_id,
            member_ids,
        )

        # Log distillation action
        await conn.execute(
            """
            INSERT INTO memory_distillation_log
                (agent, action, affected_ids, notes, model_used, duration_ms)
            VALUES ($1, $2, $3::uuid[], $4, $5, $6)
            """,
            "memory-agent",
            f"cluster-level-{level}",
            member_ids,
            f"Cluster {cluster_idx+1}/{n_clusters}: {len(members)} memories → '{summary[:100]}...'",
            model,
            duration_ms,
        )

        cluster_ids.append(cluster_id)
        logger.info(f"  Cluster {cluster_idx+1}: {len(members)} memories → ID {cluster_id}")

    return cluster_ids


async def run_raptor_pass(conn, agent_scope: str | None = None) -> dict:
    """
    Full RAPTOR pass:
    Level 0: Cluster leaf memories → Level-0 clusters (each summarizing ~10-50 memories)
    Level 1: Cluster level-0 cluster summaries → Level-1 clusters (each summarizing ~5-10 clusters)
    Level 2: If enough level-1 clusters, one final super-summary

    Returns stats dict.
    """
    model = _get_summarize_model()
    stats = {"model": model, "levels": {}}

    # Level 0: cluster leaf memories
    unclustered = await fetch_unclustered_memories(conn, agent_scope=agent_scope, limit=1000)
    logger.info(f"Found {len(unclustered)} unclustered memories")

    if len(unclustered) >= CLUSTER_SIZE_MIN:
        l0_ids = await build_clusters(conn, unclustered, level=0, agent_scope=agent_scope, model=model)
        stats["levels"][0] = {"clusters_created": len(l0_ids)}

        # Level 1: cluster the level-0 summaries
        if len(l0_ids) >= CLUSTER_SIZE_MIN:
            l0_rows = await conn.fetch(
                "SELECT id, summary AS content, embedding, agent_scope FROM memory_clusters WHERE level = 0 AND parent_id IS NULL AND agent_scope IS NOT DISTINCT FROM $1",
                agent_scope,
            )
            l0_as_mems = [dict(r) for r in l0_rows]

            if len(l0_as_mems) >= CLUSTER_SIZE_MIN:
                l1_ids = await build_clusters(
                    conn, l0_as_mems, level=1, agent_scope=agent_scope, model=model
                )
                # Link l0 clusters to l1 parents
                for l1_id in l1_ids:
                    l1_row = await conn.fetchrow(
                        "SELECT child_ids FROM memory_clusters WHERE id = $1", l1_id
                    )
                    if l1_row:
                        await conn.execute(
                            "UPDATE memory_clusters SET parent_id = $1 WHERE id = ANY($2::uuid[])",
                            l1_id,
                            l1_row["child_ids"],
                        )
                stats["levels"][1] = {"clusters_created": len(l1_ids)}

    stats["unclustered_processed"] = len(unclustered)
    return stats


async def rebuild_keyword_index(conn, limit: int = 500) -> int:
    """
    Rebuild the keyword index for fast grep-style lookup.
    Extracts top keywords from memories using simple TF-IDF approximation via PostgreSQL.
    """
    # Use PostgreSQL's ts_stat to get term frequencies across all memories
    # Then insert top-weight keywords per memory for fast lookup
    count = 0
    rows = await conn.fetch(
        """
        SELECT id, content
        FROM memories
        WHERE id NOT IN (SELECT DISTINCT memory_id FROM memory_keyword_index)
        ORDER BY created_at DESC
        LIMIT $1
        """,
        limit,
    )

    for row in rows:
        mem_id = row["id"]
        content = row["content"]

        # Extract keywords using PostgreSQL tsvector parsing
        kw_rows = await conn.fetch(
            """
            SELECT unnest(tsvector_to_array(to_tsvector('english', $1))) AS kw
            """,
            content[:2000],
        )
        keywords = [r["kw"] for r in kw_rows]

        # Simple TF scoring: count occurrences
        word_count: dict[str, int] = {}
        for kw in keywords:
            word_count[kw] = word_count.get(kw, 0) + 1

        # Insert top 20 keywords by frequency
        top_kws = sorted(word_count.items(), key=lambda x: x[1], reverse=True)[:20]
        max_count = max(c for _, c in top_kws) if top_kws else 1

        for kw, cnt in top_kws:
            weight = cnt / max_count
            await conn.execute(
                """
                INSERT INTO memory_keyword_index (memory_id, keyword, weight)
                VALUES ($1, $2, $3)
                ON CONFLICT (memory_id, keyword) DO UPDATE SET weight = $3
                """,
                mem_id,
                kw,
                weight,
            )
        count += 1

    return count


async def compact_expired_memories(conn) -> int:
    """
    Soft-compact expired memories: don't delete, but lower confidence to 0.1
    and mark in metadata so they're deprioritized in retrieval.
    This preserves history while not polluting active search results.
    """
    result = await conn.execute(
        """
        UPDATE memories
        SET
            confidence = 0.1,
            metadata = jsonb_set(
                metadata,
                '{quality}',
                jsonb_build_object(
                    'confidence', 'low',
                    'compacted_at', now()::text,
                    'reason', 'ttl_expired'
                )
            )
        WHERE
            confidence > 0.1
            AND metadata->'quality'->>'expires' IS NOT NULL
            AND (metadata->'quality'->>'expires')::timestamptz < now()
        """,
    )
    return int(result.split()[-1]) if result else 0


if __name__ == "__main__":
    import asyncpg

    async def main():
        DATABASE_URL = os.environ.get(
            "DATABASE_URL",
            os.environ.get("MEMU_DATABASE_URL", ""),
        )
        if not DATABASE_URL:
            print("ERROR: DATABASE_URL not set")
            return

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            print("=== RAPTOR Clustering Pass ===")
            stats = await run_raptor_pass(conn)
            print(json.dumps(stats, indent=2, default=str))

            print("\n=== Keyword Index Rebuild ===")
            indexed = await rebuild_keyword_index(conn, limit=500)
            print(f"Indexed keywords for {indexed} memories")

            print("\n=== Compact Expired Memories ===")
            compacted = await compact_expired_memories(conn)
            print(f"Compacted {compacted} expired memories")
        finally:
            await conn.close()

    asyncio.run(main())
