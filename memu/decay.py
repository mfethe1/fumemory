"""Memory decay and reinforcement logic."""

from __future__ import annotations

import math
from datetime import datetime, timezone


def compute_final_score(
    similarity: float,
    created_at: datetime,
    access_count: int,
    decay_rate: float = 0.01,
    temporal_weight: float = 0.3,
    min_score: float = 0.1,
) -> float:
    """Compute final ranking score blending similarity, recency, and access frequency.

    score = similarity × decay_factor × access_boost × temporal_blend

    Args:
        similarity: Cosine similarity from vector search (0-1).
        created_at: When the memory was created.
        access_count: How many times this memory has been accessed.
        decay_rate: Daily decay rate (default 1% per day).
        temporal_weight: Blend between similarity (0) and recency (1).
        min_score: Floor — memories never fully disappear.
    """
    now = datetime.now(timezone.utc)
    days_old = max((now - created_at).total_seconds() / 86400, 0)

    # Exponential decay over time
    decay_factor = max((1 - decay_rate) ** days_old, min_score)

    # Logarithmic access boost — diminishing returns
    access_boost = 1 + math.log(access_count + 1)

    # Recency score: 1.0 for brand new, decays to min_score
    recency = decay_factor

    # Blend similarity with recency
    blended = (1 - temporal_weight) * similarity + temporal_weight * recency

    return blended * access_boost * decay_factor


def should_deduplicate(similarity: float, threshold: float = 0.95) -> bool:
    """Check if a new memory is a near-duplicate of an existing one."""
    return similarity >= threshold
