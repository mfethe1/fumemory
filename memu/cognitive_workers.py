"""Phase 4: Self-Healing & Subconscious cognitive workers.

Three background workers that run as NATS consumers:

1. ContradictionEngine — detects contradicting facts, creates CONTRADICTS edges
2. ProspectiveMemoryManager — manages future-intent reminders
3. SubconsciousStream — ambient context broadcasting + injection

All workers respect context isolation:
  - Write ONLY to Primed_Cache (worker-writable)
  - NEVER write to Working_Context (agent-only)
  - Exception: ProspectiveMemory uses caller="agent" for urgent reminders
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
import numpy as np

from memu.core_memory import (
    Block,
    CoreMemoryManager,
    count_tokens,
    DEFAULT_TOKEN_LIMITS,
)

logger = logging.getLogger(__name__)

# --- Config ---
LLM_BASE_URL = os.environ.get("PROFILER_LLM_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("PROFILER_LLM_MODEL", "gpt-4o-mini")
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")

CONTRADICTION_SIMILARITY_THRESHOLD = 0.88
SUBCONSCIOUS_SIMILARITY_THRESHOLD = 0.85


# ---------------------------------------------------------------------------
# Shared LLM helper
# ---------------------------------------------------------------------------


async def _call_llm_json(system: str, user: str, max_tokens: int = 300) -> dict | None:
    """Call LLM with JSON response format."""
    if not LLM_API_KEY:
        return None
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{LLM_BASE_URL}/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return json.loads(data["choices"][0]["message"]["content"])
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 1. Contradiction Engine
# ---------------------------------------------------------------------------


class ContradictionEngine:
    """Detects contradicting facts and creates CONTRADICTS edges in Apache AGE.

    Listens to memu.events.ingested. When a new fact arrives:
      1. Vector search for high-similarity existing memories (> 0.88)
      2. ONLY if similarity exceeds threshold, invoke LLM contradiction judge
      3. If contradiction confirmed: create CONTRADICTS edge + emit swarm event
      4. Does NOT auto-delete — preserves both facts for human review
    """

    def __init__(self, core_mem: CoreMemoryManager | None = None):
        self._core_mem = core_mem
        self._subscription = None
        self._running = False

    async def start(self, nc: Any) -> None:
        """Subscribe to memu.events.ingested."""
        self._running = True
        self._subscription = await nc.subscribe("memu.events.ingested", cb=self._on_ingested)
        logger.info("ContradictionEngine started — listening on memu.events.ingested")

    async def stop(self) -> None:
        self._running = False
        if self._subscription:
            try:
                await self._subscription.unsubscribe()
            except Exception:
                pass

    async def _on_ingested(self, msg: Any) -> None:
        """Handle new memory ingestion event."""
        try:
            payload = json.loads(msg.data.decode())
        except Exception:
            return

        memory_id = payload.get("memory_id")
        content = payload.get("content", "")
        embedding = payload.get("embedding")
        agent_id = payload.get("agent_id", "")

        if not content or not embedding:
            return

        await self.check_contradiction(memory_id, content, embedding, agent_id)

    async def check_contradiction(
        self,
        memory_id: str,
        content: str,
        embedding: list[float],
        agent_id: str,
        pool: Any = None,
        nats_publisher: Any = None,
    ) -> dict | None:
        """Check if a new fact contradicts existing memories.

        Returns contradiction info dict if found, None otherwise.
        """
        if pool is None:
            return None

        # Stage 1: Vector similarity filter (> 0.88)
        candidates = await self._find_similar_facts(embedding, memory_id, pool)
        if not candidates:
            return None

        # Stage 2: LLM contradiction judge (only for high-similarity candidates)
        for candidate in candidates:
            is_contradiction = await self._judge_contradiction(content, candidate["content"])
            if is_contradiction:
                # Create CONTRADICTS edge in AGE (best-effort)
                await self._create_contradicts_edge(memory_id, candidate["id"], pool)

                # Emit swarm event (do NOT auto-delete)
                if nats_publisher:
                    try:
                        await nats_publisher.publish(
                            "memu.events.contradiction",
                            {
                                "new_memory_id": memory_id,
                                "existing_memory_id": candidate["id"],
                                "agent_id": agent_id,
                                "similarity": candidate["similarity"],
                            },
                        )
                    except Exception:
                        pass

                logger.info(
                    "Contradiction detected: %s contradicts %s (sim=%.3f)",
                    memory_id, candidate["id"], candidate["similarity"],
                )
                return {
                    "new_memory_id": memory_id,
                    "existing_memory_id": candidate["id"],
                    "similarity": candidate["similarity"],
                }

        return None

    async def _find_similar_facts(
        self, embedding: list[float], exclude_id: str, pool: Any,
    ) -> list[dict]:
        """Find existing active facts with similarity > threshold."""
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, content, 1 - (embedding <=> $1::vector) AS similarity
                    FROM memories
                    WHERE embedding IS NOT NULL
                      AND searchable = TRUE
                      AND id != $2::uuid
                    ORDER BY embedding <=> $1::vector
                    LIMIT 5
                    """,
                    str(embedding),
                    exclude_id,
                )
            return [
                {"id": str(r["id"]), "content": r["content"], "similarity": float(r["similarity"])}
                for r in rows
                if float(r["similarity"]) >= CONTRADICTION_SIMILARITY_THRESHOLD
            ]
        except Exception as exc:
            logger.warning("Contradiction similarity search failed: %s", exc)
            return []

    async def _judge_contradiction(self, fact_a: str, fact_b: str) -> bool:
        """LLM judge: do these two facts contradict each other?"""
        result = await _call_llm_json(
            "You are a fact-checking judge. Determine if two statements contradict each other.",
            f'Fact A: "{fact_a[:500]}"\nFact B: "{fact_b[:500]}"\n\n'
            'Output JSON: {"contradicts": true/false, "reasoning": "brief explanation"}',
        )
        if result is None:
            return False
        return bool(result.get("contradicts", False))

    async def _create_contradicts_edge(self, new_id: str, existing_id: str, pool: Any) -> None:
        """Create a CONTRADICTS edge in Apache AGE."""
        try:
            async with pool.acquire() as conn:
                await conn.execute("SET search_path = ag_catalog, \"$user\", public")
                cypher_sql = f"""
                SELECT * FROM cypher('memu_graph', $$
                    MERGE (new:Memory {{id: '{new_id}'}})
                    MERGE (old:Memory {{id: '{existing_id}'}})
                    CREATE (new)-[:CONTRADICTS]->(old)
                $$) AS (result agtype);
                """
                await conn.execute(cypher_sql)
        except Exception as exc:
            logger.warning("CONTRADICTS edge creation failed: %s", exc)


# ---------------------------------------------------------------------------
# 2. Subconscious Ambient Stream
# ---------------------------------------------------------------------------


AGENT_TTL_SECONDS = 300  # Evict agents inactive for 5 minutes


class SubconsciousStream:
    """Central worker that broadcasts high-value context and injects into relevant agents.

    Listens to memu.subconscious JetStream topic.
    Maintains a NumPy 2D matrix of active agent context vectors for vectorized
    cosine similarity (single ``np.dot`` across all agents — no per-agent loop).
    If similarity > 0.85, injects into that agent's Primed_Cache block.

    Agents are evicted from the matrix after AGENT_TTL_SECONDS of inactivity.
    """

    def __init__(self, core_mem: CoreMemoryManager | None = None):
        self._core_mem = core_mem
        # Ordered list of agent IDs matching rows in the matrix
        self._agent_ids: list[str] = []
        # 2D matrix: rows = agents, cols = embedding dims (lazily sized)
        self._agent_matrix: np.ndarray | None = None
        # Last-seen timestamps for TTL eviction
        self._agent_last_seen: dict[str, float] = {}
        self._subscription = None
        self._running = False
        self._eviction_task: asyncio.Task | None = None

    async def start(self, nc: Any) -> None:
        """Subscribe to memu.subconscious and start TTL eviction loop."""
        self._running = True
        self._subscription = await nc.subscribe("memu.subconscious", cb=self._on_broadcast)
        self._eviction_task = asyncio.create_task(self._eviction_loop())
        logger.info("SubconsciousStream started — listening on memu.subconscious")

    async def stop(self) -> None:
        self._running = False
        if self._eviction_task:
            self._eviction_task.cancel()
            try:
                await self._eviction_task
            except asyncio.CancelledError:
                pass
        if self._subscription:
            try:
                await self._subscription.unsubscribe()
            except Exception:
                pass

    def update_agent_vector(self, agent_id: str, embedding: list[float]) -> None:
        """Update the cached context vector for an active agent (rebuilds matrix row)."""
        vec = np.array(embedding, dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm  # Pre-normalize for fast dot-product similarity

        self._agent_last_seen[agent_id] = time.monotonic()

        if agent_id in self._agent_ids:
            idx = self._agent_ids.index(agent_id)
            self._agent_matrix[idx] = vec
        else:
            self._agent_ids.append(agent_id)
            row = vec.reshape(1, -1)
            if self._agent_matrix is None or len(self._agent_matrix) == 0:
                self._agent_matrix = row
            else:
                self._agent_matrix = np.vstack([self._agent_matrix, row])

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the active set and matrix."""
        if agent_id not in self._agent_ids:
            return
        idx = self._agent_ids.index(agent_id)
        self._agent_ids.pop(idx)
        self._agent_last_seen.pop(agent_id, None)
        if self._agent_matrix is not None and len(self._agent_matrix) > 0:
            self._agent_matrix = np.delete(self._agent_matrix, idx, axis=0)
            if len(self._agent_matrix) == 0:
                self._agent_matrix = None

    async def _eviction_loop(self) -> None:
        """Periodically evict agents that haven't refreshed within TTL."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute
                now = time.monotonic()
                stale = [
                    aid for aid, ts in self._agent_last_seen.items()
                    if now - ts > AGENT_TTL_SECONDS
                ]
                for aid in stale:
                    self.remove_agent(aid)
                    logger.debug("Evicted stale agent %s from subconscious matrix", aid)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _on_broadcast(self, msg: Any) -> None:
        """Handle a subconscious broadcast."""
        try:
            payload = json.loads(msg.data.decode())
        except Exception:
            return

        embedding = payload.get("embedding")
        summary = payload.get("summary", "")
        source_agent = payload.get("source_agent", "")

        if not embedding or not summary:
            return

        await self.check_and_inject(embedding, summary, source_agent)

    async def check_and_inject(
        self,
        embedding: list[float],
        summary: str,
        source_agent: str = "",
    ) -> list[str]:
        """Vectorized comparison: single np.dot across all agents.

        Returns list of agent_ids that received the injection.
        """
        if not self._agent_ids or self._agent_matrix is None or self._core_mem is None:
            return []

        broadcast_vec = np.array(embedding, dtype=np.float32)
        b_norm = np.linalg.norm(broadcast_vec)
        if b_norm == 0:
            return []
        broadcast_vec = broadcast_vec / b_norm

        # Vectorized cosine similarity: single matrix-vector dot product
        similarities = self._agent_matrix @ broadcast_vec  # shape: (n_agents,)

        injected = []
        # Only iterate agents that exceed threshold
        above_threshold = np.where(similarities >= SUBCONSCIOUS_SIMILARITY_THRESHOLD)[0]

        for idx in above_threshold:
            agent_id = self._agent_ids[idx]
            if agent_id == source_agent:
                continue

            similarity = float(similarities[idx])
            try:
                entry = await self._core_mem.get_block(agent_id, Block.PRIMED_CACHE)
                revision = entry.revision if entry else 0
                content = f"[Subconscious insight | sim={similarity:.2f}] {summary}"

                if count_tokens(content) <= DEFAULT_TOKEN_LIMITS[Block.PRIMED_CACHE]:
                    await self._core_mem.update_block(
                        agent_id, Block.PRIMED_CACHE, content,
                        revision, caller="worker",
                    )
                    injected.append(agent_id)
                    logger.info(
                        "Subconscious injection: %s → %s (sim=%.3f)",
                        source_agent, agent_id, similarity,
                    )
            except Exception as exc:
                logger.warning("Subconscious injection failed for %s: %s", agent_id, exc)

        return injected

