"""Archival Pager — moves data between NATS KV core memory and PostgreSQL archival.

When an agent's Working_Context overflows its token budget, the pager:
  1. Takes the oldest N paragraphs from Working_Context
  2. Writes them to PostgreSQL as a regular memory (searchable archival)
  3. Removes them from the NATS KV block

When an agent needs old context, the pager:
  1. Runs a vector search against PostgreSQL archival
  2. Injects the top results into the NATS KV Working_Context (if token budget allows)
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg

from memu.core_memory import (
    Block,
    BlockEntry,
    CoreMemoryManager,
    count_tokens,
    DEFAULT_TOKEN_LIMITS,
)

logger = logging.getLogger(__name__)

# Fraction of token limit at which to trigger eviction (80%)
EVICTION_THRESHOLD = 0.80

# Number of paragraphs to evict per cycle
EVICTION_BATCH_SIZE = 3

# Max results to page back from archival
PAGE_BACK_LIMIT = 5


async def context_to_archival(
    mgr: CoreMemoryManager,
    pool: asyncpg.Pool,
    agent_id: str,
    *,
    get_embedding: Any = None,
) -> int:
    """Evict oldest paragraphs from Working_Context to PostgreSQL archival.

    Returns the number of paragraphs evicted.
    """
    entry = await mgr.get_block(agent_id, Block.WORKING_CONTEXT)
    if entry is None or not entry.content.strip():
        return 0

    limit = DEFAULT_TOKEN_LIMITS[Block.WORKING_CONTEXT]
    threshold = int(limit * EVICTION_THRESHOLD)

    if entry.tokens <= threshold:
        return 0

    # Split into paragraphs (double-newline delimited)
    paragraphs = [p.strip() for p in entry.content.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1:
        return 0

    # Take oldest paragraphs for eviction
    to_evict = paragraphs[:EVICTION_BATCH_SIZE]
    to_keep = paragraphs[EVICTION_BATCH_SIZE:]

    # Write evicted paragraphs to PostgreSQL as archival memories
    evicted = 0
    for para in to_evict:
        embedding = None
        if get_embedding is not None:
            try:
                embedding = await get_embedding(para)
            except Exception as exc:
                logger.warning("Embedding generation failed during eviction: %s", exc)

        try:
            emb_str = str(embedding) if embedding else None
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO memories (content, agent_id, memory_type, metadata, embedding)
                    VALUES ($1, $2, 'observation',
                            '{"source": "archival_pager", "evicted_from": "Working_Context"}'::jsonb,
                            $3::vector)
                    """,
                    para,
                    agent_id,
                    emb_str,
                )
            evicted += 1
        except Exception as exc:
            logger.error("Failed to archive paragraph for %s: %s", agent_id, exc)

    # Update Working_Context with remaining paragraphs
    new_content = "\n\n".join(to_keep)
    try:
        await mgr.update_block(
            agent_id,
            Block.WORKING_CONTEXT,
            new_content,
            entry.revision,
            caller="agent",
        )
    except Exception as exc:
        logger.error("Failed to update Working_Context after eviction: %s", exc)
        return 0

    logger.info(
        "Evicted %d paragraphs from %s Working_Context (%d→%d tokens)",
        evicted, agent_id, entry.tokens, count_tokens(new_content),
    )
    return evicted


async def page_from_archival(
    mgr: CoreMemoryManager,
    pool: asyncpg.Pool,
    agent_id: str,
    query: str,
    *,
    get_embedding: Any = None,
    limit: int = PAGE_BACK_LIMIT,
) -> list[dict[str, Any]]:
    """Search PostgreSQL archival and inject top results into Working_Context.

    Returns the list of archival memories that were paged in.
    """
    embedding = None
    if get_embedding is not None:
        try:
            embedding = await get_embedding(query)
        except Exception:
            pass

    # Search archival
    async with pool.acquire() as conn:
        if embedding is not None:
            rows = await conn.fetch(
                """
                SELECT id, content, 1 - (embedding <=> $1::vector) AS similarity
                FROM memories
                WHERE agent_id = $2 AND embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                str(embedding),
                agent_id,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, content, 0.5::float8 AS similarity
                FROM memories
                WHERE agent_id = $1 AND content ILIKE $2
                ORDER BY updated_at DESC
                LIMIT $3
                """,
                agent_id,
                f"%{query}%",
                limit,
            )

    if not rows:
        return []

    # Build injection text
    paged_texts = []
    for row in rows:
        paged_texts.append(f"[archival|sim={row['similarity']:.2f}] {row['content']}")

    injection = "\n\n".join(paged_texts)

    # Check if injection fits in Working_Context budget
    entry = await mgr.get_block(agent_id, Block.WORKING_CONTEXT)
    current_tokens = entry.tokens if entry else 0
    injection_tokens = count_tokens(injection)
    budget = DEFAULT_TOKEN_LIMITS[Block.WORKING_CONTEXT]

    if current_tokens + injection_tokens > budget:
        # Trim to fit
        while paged_texts and current_tokens + count_tokens("\n\n".join(paged_texts)) > budget:
            paged_texts.pop()
        if not paged_texts:
            logger.warning("No archival results fit in %s Working_Context budget", agent_id)
            return []
        injection = "\n\n".join(paged_texts)

    # Append to Working_Context
    try:
        revision = entry.revision if entry else 0
        await mgr.append_block(
            agent_id,
            Block.WORKING_CONTEXT,
            injection,
            revision,
            caller="agent",
        )
    except Exception as exc:
        logger.error("Failed to inject archival into Working_Context: %s", exc)
        return []

    result = [{"id": str(row["id"]), "content": row["content"], "similarity": row["similarity"]} for row in rows[:len(paged_texts)]]
    logger.info("Paged %d archival memories into %s Working_Context", len(result), agent_id)
    return result

