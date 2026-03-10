from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from math import exp
from typing import Any


def reciprocal_rank_fusion(rankings: Iterable[list[tuple[str, float]]], k: int = 60) -> dict[str, float]:
    """Fuse multiple ranked lists using Reciprocal Rank Fusion.

    Each ranking item is ``(doc_id, score)``. The raw score is ignored for fusion;
    only order matters. Returns fused scores keyed by doc_id.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, (doc_id, _score) in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + (1.0 / (k + rank))
    return fused


def normalize_ranked_rows(rows: list[Any], score_key: str, id_key: str = "id") -> list[tuple[str, float]]:
    """Convert asyncpg rows/dicts into ``(id, score)`` tuples preserving order."""
    normalized: list[tuple[str, float]] = []
    for row in rows:
        doc_id = str(row[id_key])
        normalized.append((doc_id, float(row[score_key] or 0.0)))
    return normalized


def compute_graph_temporal_boost(
    memory_id: str,
    neighbor_rows: list[Any],
    now: datetime | None = None,
    decay_days: float = 14.0,
) -> float:
    """Compute a small boost from graph neighbors, weighted by recency.

    Neighbor rows are expected to expose ``strength`` and ``created_at``.
    Returns a bounded additive score suitable for re-ranking.
    """
    if not neighbor_rows:
        return 0.0

    now = now or datetime.now(timezone.utc)
    total = 0.0
    for row in neighbor_rows:
        created_at = row.get("created_at") if isinstance(row, dict) else row["created_at"]
        strength = row.get("strength") if isinstance(row, dict) else row["strength"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_days = max((now - created_at).total_seconds() / 86400.0, 0.0)
        recency = exp(-age_days / max(decay_days, 0.1))
        total += float(strength or 0.0) * recency

    # Bound the effect so graph proximity nudges ranking instead of dominating it.
    return min(total, 1.0) * 0.15


def blend_hybrid_score(
    fused_score: float,
    temporal_score: float,
    graph_boost: float,
) -> float:
    """Combine fused retrieval, temporal relevance, and graph adjacency."""
    return fused_score + (0.35 * temporal_score) + graph_boost
