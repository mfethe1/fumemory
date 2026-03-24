"""
memU Memory Agent — idle-time compaction, concept indexing, and hierarchical distillation.

Philosophy:
  - NEVER deletes memories — only adds compaction/summary nodes
  - Originals are fully preserved; compactions are queryable alongside them
  - Uses local Ollama model (zero API cost during idle cycles)
  - Concept index enables O(1) grep-like lookup without vector search
  - Inspired by MemGPT/Letta hierarchical memory + recursive LM memory consolidation

Usage:
  python3 -m memu.memory_agent --compact
  python3 -m memu.memory_agent --index
  python3 -m memu.memory_agent --distill
  python3 -m memu.memory_agent --full         (default)
  python3 -m memu.memory_agent --daemon       (run every N minutes when idle)
  python3 -m memu.memory_agent --status       (show last run stats)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [memory_agent] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("memory_agent")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://memu:memu@localhost:5432/memu")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "4096"))

# Text model preference — we probe ollama and use the first available
LLM_PREFERENCE = os.environ.get(
    "LLM_MODEL",
    "nemotron-mini:4b,nemotron:8b,llama3.2:3b,llama3:8b,qwen2.5:3b,phi3:mini,mistral:7b",
).split(",")

SIMILARITY_CLUSTER_THRESHOLD = float(os.environ.get("CLUSTER_THRESHOLD", "0.82"))


# ---------------------------------------------------------------------------
# Apache AGE Graph-per-Tenant Isolation
# ---------------------------------------------------------------------------


async def ensure_tenant_graph(conn: asyncpg.Connection, tenant_id: str | None = None) -> str:
    """Ensure the tenant-specific Apache AGE graph exists. Returns the graph name.

    Each enterprise tenant gets an isolated graph namespace to prevent
    cross-tenant knowledge graph traversal. Falls back to 'memu_default'
    for local dev and tests (when TENANT_ID is not set).
    """
    graph_name = f"tenant_{tenant_id}" if tenant_id else "memu_default"
    try:
        await conn.execute(
            "SELECT * FROM ag_catalog.create_graph($1);",
            graph_name,
        )
        log.info("Created Apache AGE graph: %s", graph_name)
    except Exception:
        pass  # Graph already exists
    return graph_name
MIN_CLUSTER_SIZE = int(os.environ.get("MIN_CLUSTER_SIZE", "3"))
MAX_CLUSTER_SIZE = int(os.environ.get("MAX_CLUSTER_SIZE", "25"))
IDLE_POLL_INTERVAL = int(os.environ.get("IDLE_POLL_SECONDS", "300"))

# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------


async def get_available_llm_model() -> Optional[str]:
    """Probe Ollama and return the first available text model from preference list."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            available = {m["name"].split(":")[0] for m in r.json().get("models", [])}
            available_full = {m["name"] for m in r.json().get("models", [])}
            log.info("Ollama models available: %s", sorted(available_full))
            for pref in LLM_PREFERENCE:
                base = pref.split(":")[0]
                # Try exact match first
                if pref in available_full:
                    return pref
                # Try base name match
                if base in available:
                    # Return full name
                    for m in r.json().get("models", []):
                        if m["name"].split(":")[0] == base:
                            return m["name"]
    except Exception as e:
        log.warning("Could not probe Ollama models: %s", e)
    return None


async def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding using Ollama qwen3-embedding (4096 dims)."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Try OpenAI-compat endpoint first
            try:
                r = await client.post(
                    f"{OLLAMA_BASE_URL}/v1/embeddings",
                    json={"input": text, "model": EMBEDDING_MODEL},
                )
                r.raise_for_status()
                data = r.json()
                emb = data.get("data", [{}])[0].get("embedding")
                if emb and len(emb) == EMBEDDING_DIMS:
                    return emb
            except Exception:
                pass

            # Legacy Ollama endpoint
            r = await client.post(
                f"{OLLAMA_BASE_URL}/api/embeddings",
                json={"model": EMBEDDING_MODEL, "prompt": text},
            )
            r.raise_for_status()
            emb = r.json().get("embedding")
            if emb and len(emb) == EMBEDDING_DIMS:
                return emb
    except Exception as e:
        log.warning("Embedding generation failed: %s", e)
    return None


async def ollama_generate(prompt: str, model: str, max_tokens: int = 512) -> str:
    """Call Ollama generate endpoint for text completion."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"num_predict": max_tokens, "temperature": 0.3},
                },
            )
            r.raise_for_status()
            return r.json().get("response", "").strip()
    except Exception as e:
        log.warning("Ollama generate failed (%s): %s", model, e)
        return ""


async def generate_summary(memories: list[str], model: str, agent_context: str = "") -> str:
    """Generate a distilled summary of a cluster of memories."""
    joined = "\n---\n".join(f"[{i+1}] {m}" for i, m in enumerate(memories[:20]))
    ctx = f"These memories are from agent: {agent_context}. " if agent_context else ""
    prompt = (
        f"{ctx}You are a memory consolidation AI. "
        f"Summarize these {len(memories)} related memories into 2-3 concise sentences "
        f"capturing the core insight, pattern, or lesson. Be specific and actionable. "
        f"Do not include meta-commentary about the summary itself.\n\n"
        f"Memories:\n{joined}\n\nSummary:"
    )
    result = await ollama_generate(prompt, model, max_tokens=300)
    if not result:
        # Fallback: simple truncation summary
        result = f"Cluster of {len(memories)} related memories: " + memories[0][:200] + "..."
    return result


async def extract_concepts(content: str, model: str) -> list[str]:
    """Extract key concepts/entities from memory content using LLM."""
    prompt = (
        "Extract 5-10 key concepts, technical terms, entities, or themes from this text. "
        "Output ONLY a comma-separated list. No explanations, no bullets, no numbering.\n\n"
        f"Text: {content[:1000]}\n\nConcepts:"
    )
    result = await ollama_generate(prompt, model, max_tokens=100)
    if not result:
        # Fallback: extract capitalized words and technical terms
        import re
        words = re.findall(r'\b[A-Z][a-z]{2,}|[a-z]{4,}\b', content)
        return list(set(words[:10]))

    concepts = [c.strip().lower() for c in result.split(",") if c.strip()]
    return concepts[:10]


# ---------------------------------------------------------------------------
# Cosine similarity (no numpy required — pure Python)
# ---------------------------------------------------------------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def cluster_by_similarity(
    memories: list[dict],
    threshold: float = SIMILARITY_CLUSTER_THRESHOLD,
    min_size: int = MIN_CLUSTER_SIZE,
    max_size: int = MAX_CLUSTER_SIZE,
) -> list[list[dict]]:
    """
    Greedy centroid-based clustering.
    Pick the memory with highest decay_score as centroid, pull in all within threshold.
    No memory appears in more than one cluster.

    memories: list of dicts with 'id', 'embedding', 'content', 'decay_score', 'agent_id'
    """
    # Filter to memories that have embeddings
    with_embeddings = [m for m in memories if m.get("embedding") and len(m["embedding"]) == EMBEDDING_DIMS]
    log.info("Clustering %d memories with embeddings (threshold=%.2f)", len(with_embeddings), threshold)

    # Sort by decay_score descending (best candidates first as centroids)
    sorted_mems = sorted(with_embeddings, key=lambda m: m.get("decay_score", 1.0), reverse=True)

    clustered_ids: set[str] = set()
    clusters: list[list[dict]] = []

    for centroid in sorted_mems:
        cid = str(centroid["id"])
        if cid in clustered_ids:
            continue

        cluster = [centroid]
        clustered_ids.add(cid)

        for candidate in sorted_mems:
            if str(candidate["id"]) in clustered_ids:
                continue
            if len(cluster) >= max_size:
                break
            sim = cosine_similarity(centroid["embedding"], candidate["embedding"])
            if sim >= threshold:
                cluster.append(candidate)
                clustered_ids.add(str(candidate["id"]))

        if len(cluster) >= min_size:
            clusters.append(cluster)

    log.info("Found %d clusters of size >= %d", len(clusters), min_size)
    return clusters


# ---------------------------------------------------------------------------
# Compaction run
# ---------------------------------------------------------------------------


async def run_compaction(pool: asyncpg.Pool, llm_model: str, run_id: str) -> dict:
    """
    Cluster similar memories and generate summary compaction nodes (level 1).
    Does NOT delete originals. Skips clusters already compacted.
    """
    stats = {"memories_processed": 0, "compactions_created": 0}

    # Load all current memories with embeddings
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, content, embedding, agent_id, memory_type,
                   decay_score, access_count, created_at
            FROM memories
            WHERE embedding IS NOT NULL AND valid_to IS NULL
            ORDER BY decay_score DESC, created_at DESC
            LIMIT 5000
            """
        )
        log.info("Loaded %d memories for clustering", len(rows))

        # Load already-compacted source IDs to avoid re-compacting
        existing = await conn.fetch(
            "SELECT source_memory_ids FROM memory_compactions WHERE compaction_level = 1"
        )

    already_compacted: set[str] = set()
    for row in existing:
        for mid in (row["source_memory_ids"] or []):
            already_compacted.add(str(mid))

    # Convert to dicts, decode embeddings
    memories = []
    for row in rows:
        mid = str(row["id"])
        if mid in already_compacted:
            continue
        emb = row["embedding"]
        if emb is None:
            continue
        # asyncpg returns vector as a string like '[0.1, 0.2, ...]'
        if isinstance(emb, str):
            try:
                emb = json.loads(emb.replace("(", "[").replace(")", "]"))
            except Exception:
                continue
        elif hasattr(emb, "tolist"):
            emb = emb.tolist()

        memories.append({
            "id": row["id"],
            "content": row["content"],
            "embedding": emb,
            "agent_id": row["agent_id"],
            "memory_type": row["memory_type"],
            "decay_score": row["decay_score"] or 1.0,
            "created_at": row["created_at"],
        })

    stats["memories_processed"] = len(memories)

    if not memories:
        log.info("No new memories to compact")
        return stats

    clusters = cluster_by_similarity(memories)

    for cluster in clusters:
        contents = [m["content"] for m in cluster]
        agent_id = cluster[0].get("agent_id") or "system"

        # Generate summary
        summary = await generate_summary(contents, llm_model, agent_context=agent_id)
        if not summary:
            continue

        # Embed summary
        embedding = await generate_embedding(summary)

        # Compute date range
        dates = [m["created_at"] for m in cluster if m.get("created_at")]
        date_start = min(dates) if dates else None
        date_end = max(dates) if dates else None

        # Extract concepts from summary
        concepts = await extract_concepts(summary, llm_model)

        source_ids = [m["id"] for m in cluster]
        emb_str = json.dumps(embedding) if embedding else None

        async with pool.acquire() as conn:
            try:
                await conn.execute(
                    """
                    INSERT INTO memory_compactions
                        (summary, embedding, agent_id, source_memory_ids, compaction_level,
                         concept_tags, date_range_start, date_range_end, metadata)
                    VALUES ($1, $2::vector, $3, $4, 1, $5, $6, $7, $8::jsonb)
                    """,
                    summary,
                    emb_str,
                    agent_id,
                    source_ids,
                    concepts,
                    date_start,
                    date_end,
                    json.dumps({
                        "cluster_size": len(cluster),
                        "llm_model": llm_model,
                        "run_id": run_id,
                    }),
                )
                stats["compactions_created"] += 1
                log.info(
                    "Compacted cluster of %d → summary: %s...",
                    len(cluster),
                    summary[:80],
                )
            except Exception as e:
                log.error("Failed to store compaction: %s", e)

    return stats


# ---------------------------------------------------------------------------
# Concept indexing run
# ---------------------------------------------------------------------------


async def run_concept_indexing(pool: asyncpg.Pool, llm_model: str, run_id: str) -> dict:
    """
    Extract concepts from all memories and build/update the concept_index.
    Upsert-safe: updates existing concept entries with new memory IDs.
    """
    stats = {"memories_processed": 0, "concepts_indexed": 0}

    async with pool.acquire() as conn:
        # Only process memories not yet in the concept index
        rows = await conn.fetch(
            """
            SELECT m.id, m.content, m.agent_id
            FROM memories m
            WHERE m.valid_to IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM memory_concept_index ci
                WHERE m.id = ANY(ci.memory_ids)
              )
            LIMIT 500
            """
        )
        log.info("Extracting concepts from %d un-indexed memories", len(rows))

    for row in rows:
        mid = row["id"]
        concepts = await extract_concepts(row["content"], llm_model)
        stats["memories_processed"] += 1

        for concept in concepts:
            concept = concept.strip().lower()
            if not concept or len(concept) < 3:
                continue
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO memory_concept_index (concept, memory_ids, frequency, last_seen)
                        VALUES ($1, ARRAY[$2]::uuid[], 1, NOW())
                        ON CONFLICT (concept) DO UPDATE SET
                            memory_ids = array_append(
                                CASE
                                    WHEN memory_concept_index.memory_ids @> ARRAY[$2]::uuid[]
                                    THEN memory_concept_index.memory_ids
                                    ELSE memory_concept_index.memory_ids
                                END,
                                CASE
                                    WHEN memory_concept_index.memory_ids @> ARRAY[$2]::uuid[]
                                    THEN NULL
                                    ELSE $2
                                END
                            ),
                            frequency = memory_concept_index.frequency + 1,
                            last_seen = NOW()
                        """,
                        concept,
                        mid,
                    )
                    stats["concepts_indexed"] += 1
            except Exception as e:
                log.debug("Concept upsert failed for '%s': %s", concept, e)

    return stats


# ---------------------------------------------------------------------------
# Distillation run (level 2: compact compactions)
# ---------------------------------------------------------------------------


async def run_distillation(pool: asyncpg.Pool, llm_model: str, run_id: str) -> dict:
    """
    Build level-2 strategic summaries from existing level-1 compactions.
    Groups compactions by agent_id, generates higher-level insights.
    """
    stats = {"memories_processed": 0, "compactions_created": 0}

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, summary, embedding, agent_id, concept_tags
            FROM memory_compactions
            WHERE compaction_level = 1
              AND NOT EXISTS (
                SELECT 1 FROM memory_compactions l2
                WHERE l2.compaction_level = 2
                  AND l2.source_compaction_ids @> ARRAY[memory_compactions.id]
              )
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
        log.info("Found %d level-1 compactions for distillation", len(rows))

    if len(rows) < MIN_CLUSTER_SIZE:
        log.info("Not enough level-1 compactions to distill (need %d, have %d)", MIN_CLUSTER_SIZE, len(rows))
        return stats

    # Group by agent_id
    by_agent: dict[str, list[dict]] = {}
    for row in rows:
        aid = row["agent_id"] or "system"
        if aid not in by_agent:
            by_agent[aid] = []
        emb = row["embedding"]
        if isinstance(emb, str):
            try:
                emb = json.loads(emb.replace("(", "[").replace(")", "]"))
            except Exception:
                emb = None
        elif hasattr(emb, "tolist"):
            emb = emb.tolist()
        by_agent[aid].append({
            "id": row["id"],
            "content": row["summary"],
            "embedding": emb,
            "agent_id": aid,
            "decay_score": 1.0,
            "created_at": None,
        })

    for agent_id, agent_comps in by_agent.items():
        valid_comps = [c for c in agent_comps if c.get("embedding")]
        if len(valid_comps) < MIN_CLUSTER_SIZE:
            continue

        clusters = cluster_by_similarity(valid_comps, threshold=0.78, min_size=2)

        for cluster in clusters:
            contents = [m["content"] for m in cluster]
            prompt = (
                f"You are a strategic memory distillation AI for agent: {agent_id}. "
                f"These {len(contents)} summaries represent related patterns or lessons. "
                f"Write a 2-3 sentence strategic insight capturing the overarching theme, "
                f"key pattern, or actionable principle. Be highly specific.\n\n"
                f"Summaries:\n" + "\n---\n".join(f"[{i+1}] {c}" for i, c in enumerate(contents))
                + "\n\nStrategic insight:"
            )
            summary = await ollama_generate(prompt, llm_model, max_tokens=250)
            if not summary:
                continue

            embedding = await generate_embedding(summary)
            concepts = await extract_concepts(summary, llm_model)
            source_compaction_ids = [m["id"] for m in cluster]

            async with pool.acquire() as conn:
                try:
                    emb_str = json.dumps(embedding) if embedding else None
                    await conn.execute(
                        """
                        INSERT INTO memory_compactions
                            (summary, embedding, agent_id, source_memory_ids, source_compaction_ids,
                             compaction_level, concept_tags, metadata)
                        VALUES ($1, $2::vector, $3, '{}', $4, 2, $5, $6::jsonb)
                        """,
                        summary,
                        emb_str,
                        agent_id,
                        source_compaction_ids,
                        concepts,
                        json.dumps({"cluster_size": len(cluster), "llm_model": llm_model, "run_id": run_id}),
                    )
                    stats["compactions_created"] += 1
                    stats["memories_processed"] += len(cluster)
                    log.info("Level-2 distillation for %s: %s...", agent_id, summary[:80])
                except Exception as e:
                    log.error("Failed to store level-2 compaction: %s", e)

    return stats


# ---------------------------------------------------------------------------
# Run log helpers
# ---------------------------------------------------------------------------


async def start_run(pool: asyncpg.Pool, run_type: str, model: str) -> str:
    run_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO memory_agent_runs (id, run_type, status, model_used)
                VALUES ($1, $2, 'running', $3)
                """,
                run_id,
                run_type,
                model,
            )
        except Exception as e:
            log.warning("Failed to record run start: %s", e)
    return run_id


async def finish_run(pool: asyncpg.Pool, run_id: str, stats: dict, error: str | None = None):
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                UPDATE memory_agent_runs SET
                    status = $2,
                    memories_processed = $3,
                    compactions_created = $4,
                    concepts_indexed = $5,
                    finished_at = NOW(),
                    error = $6
                WHERE id = $1
                """,
                run_id,
                "failed" if error else "done",
                stats.get("memories_processed", 0),
                stats.get("compactions_created", 0),
                stats.get("concepts_indexed", 0),
                error,
            )
        except Exception as e:
            log.warning("Failed to record run finish: %s", e)


async def is_run_in_progress(pool: asyncpg.Pool) -> bool:
    """Check if another agent run started in the last 30 minutes is still 'running'."""
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                SELECT id FROM memory_agent_runs
                WHERE status = 'running'
                  AND started_at > NOW() - INTERVAL '30 minutes'
                LIMIT 1
                """
            )
            return row is not None
        except Exception:
            return False


async def print_status(pool: asyncpg.Pool):
    """Print recent run stats."""
    async with pool.acquire() as conn:
        try:
            runs = await conn.fetch(
                """
                SELECT run_type, status, memories_processed, compactions_created,
                       concepts_indexed, model_used, started_at, finished_at
                FROM memory_agent_runs
                ORDER BY started_at DESC LIMIT 10
                """
            )
            mem_count = await conn.fetchval("SELECT COUNT(*) FROM memories WHERE valid_to IS NULL")
            comp_count = await conn.fetchval("SELECT COUNT(*) FROM memory_compactions")
            concept_count = await conn.fetchval("SELECT COUNT(*) FROM memory_concept_index")
        except Exception as e:
            log.error("Status query failed: %s", e)
            return

    print(f"\n=== memU Memory Agent Status ===")
    print(f"Memories:    {mem_count}")
    print(f"Compactions: {comp_count}")
    print(f"Concepts:    {concept_count}")
    print(f"\nLast 10 runs:")
    for run in runs:
        duration = ""
        if run["finished_at"] and run["started_at"]:
            secs = (run["finished_at"] - run["started_at"]).total_seconds()
            duration = f" ({int(secs)}s)"
        print(
            f"  {run['started_at'].strftime('%Y-%m-%d %H:%M')} "
            f"{run['run_type']:12} {run['status']:8} "
            f"mem={run['memories_processed']} comp={run['compactions_created']} "
            f"concepts={run['concepts_indexed']} model={run['model_used']}{duration}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def full_run(pool: asyncpg.Pool, llm_model: str):
    """Run compaction → indexing → distillation in sequence."""
    all_stats = {"memories_processed": 0, "compactions_created": 0, "concepts_indexed": 0}

    if await is_run_in_progress(pool):
        log.info("Another run is in progress — skipping")
        return

    run_id = await start_run(pool, "full", llm_model)
    log.info("Starting full memory agent run (id=%s, model=%s)", run_id, llm_model)
    error = None

    try:
        log.info("--- Phase 1: Compaction ---")
        s1 = await run_compaction(pool, llm_model, run_id)
        all_stats["memories_processed"] += s1.get("memories_processed", 0)
        all_stats["compactions_created"] += s1.get("compactions_created", 0)

        log.info("--- Phase 2: Concept Indexing ---")
        s2 = await run_concept_indexing(pool, llm_model, run_id)
        all_stats["memories_processed"] += s2.get("memories_processed", 0)
        all_stats["concepts_indexed"] += s2.get("concepts_indexed", 0)

        log.info("--- Phase 3: Distillation ---")
        s3 = await run_distillation(pool, llm_model, run_id)
        all_stats["compactions_created"] += s3.get("compactions_created", 0)

    except Exception as e:
        error = str(e)
        log.error("Full run failed: %s", e)
    finally:
        await finish_run(pool, run_id, all_stats, error)
        log.info("Full run complete: %s", all_stats)

    return all_stats


async def daemon_loop(pool: asyncpg.Pool):
    """Run as a daemon, triggering full runs every IDLE_POLL_INTERVAL seconds."""
    log.info("Daemon mode: polling every %ds", IDLE_POLL_INTERVAL)
    while True:
        llm_model = await get_available_llm_model()
        if llm_model:
            log.info("Using LLM model: %s", llm_model)
            await full_run(pool, llm_model)
        else:
            log.warning("No LLM model available in Ollama — skipping this cycle")
        await asyncio.sleep(IDLE_POLL_INTERVAL)


async def main():
    parser = argparse.ArgumentParser(description="memU Memory Agent")
    parser.add_argument("--compact", action="store_true", help="Run compaction only")
    parser.add_argument("--index", action="store_true", help="Run concept indexing only")
    parser.add_argument("--distill", action="store_true", help="Run distillation only")
    parser.add_argument("--full", action="store_true", help="Run all phases (default)")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon")
    parser.add_argument("--status", action="store_true", help="Show run status")
    parser.add_argument("--model", help="Override LLM model")
    parser.add_argument("--threshold", type=float, default=SIMILARITY_CLUSTER_THRESHOLD,
                        help="Cosine similarity threshold for clustering")
    args = parser.parse_args()

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5)

    if args.status:
        await print_status(pool)
        await pool.close()
        return

    llm_model = args.model
    if not llm_model:
        llm_model = await get_available_llm_model()
        if not llm_model:
            log.error(
                "No LLM model found in Ollama. Pull one first:\n"
                "  ollama pull nemotron-mini:4b  # fastest, ~2.7GB\n"
                "  ollama pull llama3.2:3b        # good alternative\n"
                "  ollama pull qwen2.5:3b         # multilingual capable"
            )
            sys.exit(1)

    log.info("Using LLM model: %s", llm_model)

    if args.daemon:
        await daemon_loop(pool)
        return

    if args.compact:
        run_id = await start_run(pool, "compaction", llm_model)
        stats = await run_compaction(pool, llm_model, run_id)
        await finish_run(pool, run_id, stats)
        log.info("Compaction complete: %s", stats)
    elif args.index:
        run_id = await start_run(pool, "indexing", llm_model)
        stats = await run_concept_indexing(pool, llm_model, run_id)
        await finish_run(pool, run_id, stats)
        log.info("Indexing complete: %s", stats)
    elif args.distill:
        run_id = await start_run(pool, "distillation", llm_model)
        stats = await run_distillation(pool, llm_model, run_id)
        await finish_run(pool, run_id, stats)
        log.info("Distillation complete: %s", stats)
    else:
        # Default: full run
        await full_run(pool, llm_model)

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())


# ===========================================================================
# Phase 2: Semantic Ingestion Pipeline — Triplet Extraction + Entity Resolution
# ===========================================================================

from enum import Enum as StdEnum
from pydantic import BaseModel as PydanticBaseModel, Field as PydanticField

import httpx


class RelationshipType(str, StdEnum):
    """Allowed relationship types for the knowledge graph.
    ALL triplet predicates MUST be one of these values.
    """
    RELATES_TO = "RELATES_TO"
    DEPENDS_ON = "DEPENDS_ON"
    CAUSES = "CAUSES"
    CAUSED_BY = "CAUSED_BY"
    PART_OF = "PART_OF"
    CONTAINS = "CONTAINS"
    USES = "USES"
    USED_BY = "USED_BY"
    CREATED_BY = "CREATED_BY"
    CREATES = "CREATES"
    MODIFIES = "MODIFIES"
    MODIFIED_BY = "MODIFIED_BY"
    TRIGGERS = "TRIGGERS"
    TRIGGERED_BY = "TRIGGERED_BY"
    SUPERSEDES = "SUPERSEDES"
    CONTRADICTS = "CONTRADICTS"
    DERIVED_FROM = "DERIVED_FROM"
    SIMILAR_TO = "SIMILAR_TO"
    INSTANCE_OF = "INSTANCE_OF"
    OWNS = "OWNS"
    OWNED_BY = "OWNED_BY"
    COMMUNICATES_WITH = "COMMUNICATES_WITH"
    LOCATED_IN = "LOCATED_IN"
    OCCURRED_AT = "OCCURRED_AT"
    PRECEDED_BY = "PRECEDED_BY"
    FOLLOWED_BY = "FOLLOWED_BY"


class Triplet(PydanticBaseModel):
    subject: str = PydanticField(..., min_length=1, max_length=256)
    predicate: RelationshipType
    object: str = PydanticField(..., min_length=1, max_length=256)
    confidence: float = PydanticField(0.8, ge=0.0, le=1.0)


class TripletExtractionResult(PydanticBaseModel):
    triplets: list[Triplet] = PydanticField(default_factory=list)


class EntityResolutionResult(PydanticBaseModel):
    is_same_entity: bool
    reasoning: str = ""


TRIPLET_LLM_BASE_URL = os.environ.get("TRIPLET_LLM_URL", os.environ.get("PROFILER_LLM_URL", "https://api.openai.com/v1"))
TRIPLET_LLM_MODEL = os.environ.get("TRIPLET_LLM_MODEL", os.environ.get("PROFILER_LLM_MODEL", "gpt-4o-mini"))
TRIPLET_LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ENTITY_SIMILARITY_THRESHOLD = 0.90
MAX_TRIPLETS_PER_TEXT = 10

EXTRACTION_SYSTEM_PROMPT = """\
You are a knowledge graph extraction engine. Extract [Subject, Predicate, Object] triplets.
Predicate MUST be one of: {predicates}
Output JSON: {{"triplets": [{{"subject": "...", "predicate": "...", "object": "...", "confidence": 0.0-1.0}}]}}
"""

ENTITY_RESOLUTION_PROMPT = """\
Are these two entities the exact same thing?
Entity A: "{entity_a}" | Entity B: "{entity_b}"
Output JSON: {{"is_same_entity": true/false, "reasoning": "..."}}
"""


async def _call_triplet_llm_json(system: str, user: str, max_tokens: int = 1000) -> dict | None:
    if not TRIPLET_LLM_API_KEY:
        return None
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TRIPLET_LLM_API_KEY}"}
    payload = {
        "model": TRIPLET_LLM_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{TRIPLET_LLM_BASE_URL}/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:
        log.warning("Triplet LLM call failed: %s", exc)
        return None


async def extract_triplets(text: str) -> list[Triplet]:
    predicates = ", ".join(r.value for r in RelationshipType)
    system = EXTRACTION_SYSTEM_PROMPT.format(predicates=predicates)
    result = await _call_triplet_llm_json(system, f"Extract triplets:\n\n{text[:4000]}")
    if result is None:
        return []
    try:
        return TripletExtractionResult.model_validate(result).triplets[:MAX_TRIPLETS_PER_TEXT]
    except Exception:
        return []


async def find_similar_entity(entity_name, get_embedding, pool, threshold=ENTITY_SIMILARITY_THRESHOLD):
    embedding = None
    if get_embedding:
        try:
            embedding = await get_embedding(entity_name)
        except Exception:
            return None
    if embedding is None or pool is None:
        return None
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, content, 1 - (embedding <=> $1::vector) AS similarity FROM memories WHERE embedding IS NOT NULL ORDER BY embedding <=> $1::vector LIMIT 1",
            str(embedding),
        )
    if not rows:
        return None
    row = rows[0]
    return {"id": str(row["id"]), "content": row["content"], "similarity": float(row["similarity"])} if row["similarity"] >= threshold else None


async def verify_entity_match(entity_a, entity_b, context_a="", context_b=""):
    prompt = ENTITY_RESOLUTION_PROMPT.format(entity_a=entity_a, entity_b=entity_b)
    result = await _call_triplet_llm_json("You are an entity resolution judge.", prompt, 200)
    if result is None:
        return False
    try:
        return EntityResolutionResult.model_validate(result).is_same_entity
    except Exception:
        return False


async def resolve_and_get_node_id(entity_name, context, get_embedding, pool):
    candidate = await find_similar_entity(entity_name, get_embedding, pool)
    if candidate is None:
        return entity_name, False
    is_same = await verify_entity_match(entity_name, candidate["content"], context, candidate["content"])
    if is_same:
        return candidate["content"], True
    return entity_name, False