"""Semantic Router — Intent-based query routing for search (< 50ms, no LLM).

Pre-computes embeddings for Intent Anchors at startup. At query time,
embeds the query and uses fast numpy cosine similarity to route to the
optimal search engine: Graph, Vector, or FTS.

This replaces brute-force hybrid search with targeted routing.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search targets
# ---------------------------------------------------------------------------


class SearchTarget(str, Enum):
    """Which search engine to route to."""
    VECTOR = "vector"       # pgvector semantic search
    GRAPH = "graph"         # Apache AGE graph traversal
    FTS = "fts"             # PostgreSQL full-text / ILIKE search
    HYBRID = "hybrid"       # Fall back to brute-force all engines


# ---------------------------------------------------------------------------
# Intent Anchors — pre-defined query archetypes
# ---------------------------------------------------------------------------

# Each anchor maps a natural-language intent description to a search target.
# At startup, we embed these descriptions. At query time, we find the
# closest anchor via cosine similarity and route accordingly.

INTENT_ANCHORS: list[tuple[str, SearchTarget]] = [
    # Graph-oriented intents (relational topology)
    ("What is related to X? Show me connections and relationships.", SearchTarget.GRAPH),
    ("How are these entities connected? Show the path between them.", SearchTarget.GRAPH),
    ("What depends on this component? Show dependency graph.", SearchTarget.GRAPH),
    ("What caused this event? Show the causal chain.", SearchTarget.GRAPH),
    ("Show me all entities that interact with this system.", SearchTarget.GRAPH),

    # Vector-oriented intents (semantic similarity)
    ("Find memories similar to this concept or idea.", SearchTarget.VECTOR),
    ("What do we know about this topic? Semantic search.", SearchTarget.VECTOR),
    ("Recall past experiences related to this situation.", SearchTarget.VECTOR),
    ("Search for context about this problem or error.", SearchTarget.VECTOR),
    ("Find relevant background information on this subject.", SearchTarget.VECTOR),

    # FTS-oriented intents (exact keyword / name lookup)
    ("Find the exact error message containing this text.", SearchTarget.FTS),
    ("Search for a specific file name or path.", SearchTarget.FTS),
    ("Look up this exact configuration value or setting.", SearchTarget.FTS),
    ("Find logs containing this specific string.", SearchTarget.FTS),
    ("Search for a specific function or variable name.", SearchTarget.FTS),
]

# Minimum similarity to an anchor to route confidently.
# Below this, fall back to hybrid search.
ROUTING_CONFIDENCE_THRESHOLD = 0.55


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class SemanticRouter:
    """Routes queries to the optimal search engine using pre-computed intent anchors.

    Usage::

        router = SemanticRouter()
        await router.initialize(get_embedding)  # call once at startup
        target = router.route(query_embedding)   # < 1ms per call
    """

    def __init__(self):
        self._anchor_embeddings: np.ndarray | None = None  # shape: (N, dims)
        self._anchor_targets: list[SearchTarget] = []
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    async def initialize(self, get_embedding: Any) -> bool:
        """Pre-compute embeddings for all intent anchors.

        Args:
            get_embedding: Async callable that takes text and returns list[float] | None.

        Returns:
            True if initialization succeeded.
        """
        import asyncio
        embeddings = []
        targets = []

        tasks = [get_embedding(description) for description, _ in INTENT_ANCHORS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (description, target), result in zip(INTENT_ANCHORS, results):
            if isinstance(result, Exception):
                logger.warning("Failed to embed anchor '%s': %s", description[:50], result)
            elif result is not None:
                embeddings.append(result)
                targets.append(target)

        if not embeddings:
            logger.warning("SemanticRouter: no anchors embedded, routing disabled")
            return False

        self._anchor_embeddings = np.array(embeddings, dtype=np.float32)
        # Normalize for fast cosine similarity
        norms = np.linalg.norm(self._anchor_embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        self._anchor_embeddings = self._anchor_embeddings / norms

        self._anchor_targets = targets
        self._initialized = True
        logger.info(
            "SemanticRouter initialized with %d anchors (%d dims)",
            len(targets), self._anchor_embeddings.shape[1],
        )
        return True

    def route(self, query_embedding: list[float] | None) -> SearchTarget:
        """Route a query to the best search engine.

        Args:
            query_embedding: The query's embedding vector.

        Returns:
            The SearchTarget to use. Falls back to HYBRID if routing
            is not initialized or confidence is below threshold.
        """
        if not self._initialized or query_embedding is None or self._anchor_embeddings is None:
            return SearchTarget.HYBRID

        start = time.monotonic()

        q = np.array(query_embedding, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return SearchTarget.HYBRID
        q = q / q_norm

        # Cosine similarity = dot product of normalized vectors
        similarities = self._anchor_embeddings @ q
        best_idx = int(np.argmax(similarities))
        best_sim = float(similarities[best_idx])

        elapsed_ms = (time.monotonic() - start) * 1000

        if best_sim < ROUTING_CONFIDENCE_THRESHOLD:
            logger.debug(
                "Router: low confidence (%.3f < %.3f), falling back to HYBRID (%.1fms)",
                best_sim, ROUTING_CONFIDENCE_THRESHOLD, elapsed_ms,
            )
            return SearchTarget.HYBRID

        target = self._anchor_targets[best_idx]
        logger.debug(
            "Router: %s (confidence=%.3f, %.1fms)",
            target.value, best_sim, elapsed_ms,
        )
        return target

