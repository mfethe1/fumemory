"""Spreading Activation — Bounded associative priming via graph neighbors.

When a search returns a graph node, this module:
  1. Fetches 1-hop neighbors from Apache AGE
  2. Limits to top 5 by salience (or recency if salience not available)
  3. Writes them to the agent's Primed_Cache NATS KV block

This implements the "associative priming" concept: recalling "Postgres"
automatically warms up "pgvector", "port 5432", etc.

Context isolation: writes ONLY to Primed_Cache (worker-writable block).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from memu.core_memory import (
    Block,
    CoreMemoryManager,
    count_tokens,
    DEFAULT_TOKEN_LIMITS,
)

logger = logging.getLogger(__name__)

# Max neighbors to prime
MAX_PRIMED_NEIGHBORS = 5

# Max tokens for the primed cache content
PRIMED_CACHE_TOKEN_LIMIT = DEFAULT_TOKEN_LIMITS[Block.PRIMED_CACHE]


async def fetch_graph_neighbors(
    memory_id: str,
    pool: Any,
    graph_name: str = "memu_graph",
    limit: int = MAX_PRIMED_NEIGHBORS,
) -> list[dict[str, Any]]:
    """Fetch 1-hop neighbors from Apache AGE for a given memory/entity node.

    Returns list of dicts with keys: name, rel_type, properties.
    """
    if pool is None:
        return []

    try:
        async with pool.acquire() as conn:
            await conn.execute("SET search_path = ag_catalog, \"$user\", public")

            # Try matching by memory ID first, then by entity name
            id_esc = memory_id.replace("\\", "\\\\").replace("'", "\\'")
            cypher_sql = f"""
            SELECT * FROM cypher('{graph_name}', $$
                MATCH (m {{id: '{id_esc}'}})-[r]-(neighbor)
                RETURN neighbor.name AS name, type(r) AS rel_type,
                       neighbor.id AS neighbor_id
                LIMIT {limit * 2}
            $$) AS (name agtype, rel_type agtype, neighbor_id agtype);
            """
            rows = await conn.fetch(cypher_sql)

            if not rows:
                # Try matching by entity name
                cypher_sql = f"""
                SELECT * FROM cypher('{graph_name}', $$
                    MATCH (m:Entity {{name: '{id_esc}'}})-[r]-(neighbor)
                    RETURN neighbor.name AS name, type(r) AS rel_type,
                           neighbor.id AS neighbor_id
                    LIMIT {limit * 2}
                $$) AS (name agtype, rel_type agtype, neighbor_id agtype);
                """
                rows = await conn.fetch(cypher_sql)

            neighbors = []
            for row in rows:
                name = str(row["name"]).strip('"') if row["name"] else ""
                rel_type = str(row["rel_type"]).strip('"') if row["rel_type"] else ""
                if name:
                    neighbors.append({
                        "name": name,
                        "rel_type": rel_type,
                        "neighbor_id": str(row.get("neighbor_id", "")).strip('"'),
                    })

            # Deduplicate by name
            seen = set()
            unique = []
            for n in neighbors:
                if n["name"] not in seen:
                    seen.add(n["name"])
                    unique.append(n)

            return unique[:limit]

    except Exception as exc:
        logger.warning("Failed to fetch graph neighbors for %s: %s", memory_id, exc)
        return []


async def prime_agent_cache(
    agent_id: str,
    neighbors: list[dict[str, Any]],
    core_mem: CoreMemoryManager,
) -> bool:
    """Write neighbor context to agent's Primed_Cache block (CAS-safe).

    This overwrites the entire Primed_Cache with the new primed context.
    Only background workers may write to Primed_Cache (context isolation).
    """
    if not neighbors or core_mem is None:
        return False

    # Format primed context
    lines = ["[Primed Context — associative neighbors]"]
    for n in neighbors:
        lines.append(f"• {n['name']} ({n['rel_type']})")

    content = "\n".join(lines)

    # Token budget check
    tokens = count_tokens(content)
    if tokens > PRIMED_CACHE_TOKEN_LIMIT:
        # Trim neighbors until it fits
        while neighbors and count_tokens("\n".join(lines)) > PRIMED_CACHE_TOKEN_LIMIT:
            neighbors.pop()
            lines = ["[Primed Context — associative neighbors]"]
            for n in neighbors:
                lines.append(f"• {n['name']} ({n['rel_type']})")
        content = "\n".join(lines)

    try:
        # Read current revision for CAS
        entry = await core_mem.get_block(agent_id, Block.PRIMED_CACHE)
        revision = entry.revision if entry else 0

        await core_mem.update_block(
            agent_id,
            Block.PRIMED_CACHE,
            content,
            revision,
            caller="worker",
        )
        logger.info(
            "Primed %d neighbors into %s Primed_Cache",
            len(neighbors), agent_id,
        )
        return True
    except Exception as exc:
        logger.warning("Failed to prime cache for %s: %s", agent_id, exc)
        return False


async def activate_spreading(
    agent_id: str,
    memory_id: str,
    pool: Any,
    core_mem: CoreMemoryManager,
    graph_name: str = "memu_graph",
) -> int:
    """Full spreading activation pipeline: fetch neighbors → prime cache.

    Returns the number of neighbors primed.
    """
    neighbors = await fetch_graph_neighbors(memory_id, pool, graph_name)
    if not neighbors:
        return 0

    success = await prime_agent_cache(agent_id, neighbors, core_mem)
    return len(neighbors) if success else 0

