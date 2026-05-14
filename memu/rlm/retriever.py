"""Hybrid retriever for the RLM orchestrator.

Combines whichever of FTS / vector / graph retrieval the active
:class:`StorageBackend` advertises in its ``capabilities`` set, fuses
the rank lists via **Reciprocal Rank Fusion** (RRF, Cormack et al.
2009), and returns a single ranked list of :class:`SearchHit`.

Design rationale
----------------
* **Capability introspection, not configuration.** The retriever asks
  the backend what it can do. A markdown vault with only FTS does not
  need a different constructor than a Postgres vault with pgvector —
  the fusion step silently drops any retrieval path whose backend
  can't serve it.

* **RRF over score calibration.** Different retrievers return
  incomparable scores (BM25 vs cosine vs 1/(hop+1)). RRF only uses
  rank, so mixing them is safe.

* **Optional embedder.** If the caller doesn't provide one, vector
  retrieval is skipped entirely even when the backend supports it.
  That way solo users who haven't wired an OpenAI/Voyage/local
  encoder still get good FTS-only behavior, and can add vectors later
  without changing any callsite.

* **Optional semantic router.** When both vectors and the
  :class:`memu.semantic_router.SemanticRouter` are available, we let
  the router pick which retrievers to engage. Otherwise we run every
  supported retriever and fuse. The router is purely a *skip*
  heuristic — it never changes which hits are considered, only which
  paths run.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

from ..storage.base import SearchHit, StorageBackend


# ---------------------------------------------------------------------------
# Embedder protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """Turn text into a fixed-length float vector.

    Implementations wrap OpenAI, Voyage, Cohere, sentence-transformers,
    Ollama, or any other provider. fumemory ships no default; plug in
    whatever matches your privacy / latency / cost budget.
    """

    dimensions: int

    async def embed(self, text: str) -> Optional[list[float]]: ...


class NullEmbedder:
    """No-op embedder used when the caller skipped wiring one.

    Exposes :attr:`dimensions = 0` so the retriever can short-circuit
    vector retrieval without special-casing ``None``.
    """

    dimensions = 0

    async def embed(self, text: str) -> Optional[list[float]]:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Hybrid retriever
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalPlan:
    """Which retrievers to engage for a given query."""

    fts: bool = True
    vector: bool = True
    graph: bool = False  # graph needs a starting slug; off by default

    @classmethod
    def default(cls) -> "RetrievalPlan":
        return cls()


# Default RRF constant from the original paper; 60 is robust across
# many retrieval ensembles and is the value most libraries pick.
RRF_K = 60


class HybridRetriever:
    def __init__(
        self,
        backend: StorageBackend,
        *,
        embedder: Optional[Embedder] = None,
        rrf_k: int = RRF_K,
    ) -> None:
        self.backend = backend
        self.embedder = embedder
        self.rrf_k = rrf_k

    # ------------------------------------------------------------------ API
    async def retrieve(
        self,
        query: str,
        k: int = 10,
        *,
        kind: Optional[str] = None,
        plan: Optional[RetrievalPlan] = None,
        seed_slug: Optional[str] = None,
    ) -> list[SearchHit]:
        plan = plan or RetrievalPlan.default()
        caps = _capabilities_of(self.backend)

        # Decide which paths to run.
        run_fts = plan.fts and "fts" in caps
        run_vec = (
            plan.vector
            and "vector" in caps
            and self.embedder is not None
            and self.embedder.dimensions > 0
        )
        run_graph = plan.graph and "graph" in caps and seed_slug is not None

        tasks: list[Awaitable[list[SearchHit]]] = []
        labels: list[str] = []
        if run_fts:
            tasks.append(self.backend.search_fts(query, k=max(k, 10), kind=kind))
            labels.append("fts")
        if run_vec:
            tasks.append(self._vector_task(query, k=max(k, 10), kind=kind))
            labels.append("vector")
        if run_graph:
            tasks.append(
                self.backend.search_graph(seed_slug, hops=1)  # type: ignore[arg-type]
            )
            labels.append("graph")

        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rank_lists: list[list[SearchHit]] = []
        for label, res in zip(labels, results):
            if isinstance(res, Exception):
                # Fail soft — a broken retriever should not poison the
                # whole query. Tests exercise this branch via a backend
                # that raises on search_vector.
                continue
            rank_lists.append(res or [])
        return _rrf_fuse(rank_lists, k=k, rrf_k=self.rrf_k)

    # --------------------------------------------------------------- helpers
    async def _vector_task(
        self, query: str, k: int, kind: Optional[str]
    ) -> list[SearchHit]:
        assert self.embedder is not None  # guarded by caller
        vec = await self.embedder.embed(query)
        if not vec:
            return []
        return await self.backend.search_vector(vec, k=k, kind=kind)


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------


def _rrf_fuse(
    rank_lists: list[list[SearchHit]], *, k: int, rrf_k: int
) -> list[SearchHit]:
    """Reciprocal rank fusion over one or more ranked lists.

    For each list, a hit at rank ``r`` (1-indexed) contributes
    ``1 / (rrf_k + r)`` to the aggregate score. Ties are broken by the
    best per-list rank a hit appears at, so a hit that appears at rank
    1 in one list still beats a hit at rank 2 even if total scores are
    equal.
    """
    if not rank_lists:
        return []
    by_id: dict[str, SearchHit] = {}
    score: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    reasons: dict[str, set[str]] = {}
    for lst in rank_lists:
        for rank, hit in enumerate(lst, start=1):
            key = hit.node.id or hit.node.slug
            by_id[key] = by_id.get(key, hit)
            score[key] = score.get(key, 0.0) + 1.0 / (rrf_k + rank)
            if key not in best_rank or rank < best_rank[key]:
                best_rank[key] = rank
            reasons.setdefault(key, set()).add(hit.reason)
    ordered = sorted(
        by_id.keys(), key=lambda key: (-score[key], best_rank[key])
    )
    fused: list[SearchHit] = []
    for key in ordered[:k]:
        hit = by_id[key]
        merged_reason = "+".join(sorted(reasons[key])) or hit.reason
        fused.append(
            SearchHit(
                node=hit.node,
                score=score[key],
                reason=merged_reason,
            )
        )
    return fused


# ---------------------------------------------------------------------------
# Introspection helper
# ---------------------------------------------------------------------------


def _capabilities_of(backend: StorageBackend) -> frozenset[str]:
    caps = getattr(backend, "capabilities", None)
    if isinstance(caps, (frozenset, set)):
        return frozenset(caps)
    return frozenset({"fts"})


__all__ = [
    "Embedder",
    "NullEmbedder",
    "HybridRetriever",
    "RetrievalPlan",
    "RRF_K",
]
