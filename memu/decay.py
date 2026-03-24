"""Memory decay and reinforcement logic.

Salience-weighted Ebbinghaus decay:
  effective_decay_rate = base_rate × (1.0 - salience_score)

  - salience_score = 0.0 → full decay (routine memory)
  - salience_score = 0.5 → half decay rate (default)
  - salience_score = 0.99 → near-zero decay (critical/flashbulb memory)
  - salience_score = 1.0 → zero decay (permanent memory)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def compute_effective_decay_rate(
    base_rate: float,
    salience_score: float,
) -> float:
    """Compute salience-weighted decay rate.

    Higher salience → lower effective decay → memory persists longer.

    Args:
        base_rate: Base daily decay rate (e.g., 0.01 = 1% per day).
        salience_score: 0.0 (routine) to 1.0 (permanent).

    Returns:
        Effective decay rate, clamped to [0.0, base_rate].
    """
    salience_score = max(0.0, min(1.0, salience_score))
    return base_rate * (1.0 - salience_score)


def compute_final_score(
    similarity: float,
    created_at: datetime,
    access_count: int,
    decay_rate: float = 0.01,
    temporal_weight: float = 0.3,
    min_score: float = 0.1,
    salience_score: float = 0.5,
) -> float:
    """Compute final ranking score blending similarity, recency, and access frequency.

    score = similarity × decay_factor × access_boost × temporal_blend

    The decay rate is modulated by salience_score:
      effective_rate = decay_rate × (1.0 - salience_score)

    Args:
        similarity: Cosine similarity from vector search (0-1).
        created_at: When the memory was created.
        access_count: How many times this memory has been accessed.
        decay_rate: Base daily decay rate (default 1% per day).
        temporal_weight: Blend between similarity (0) and recency (1).
        min_score: Floor — memories never fully disappear.
        salience_score: 0.0 (routine) to 1.0 (permanent/flashbulb).
    """
    now = datetime.now(timezone.utc)
    days_old = max((now - created_at).total_seconds() / 86400, 0)

    # Salience-weighted decay rate
    effective_rate = compute_effective_decay_rate(decay_rate, salience_score)

    # Exponential decay over time
    decay_factor = max((1 - effective_rate) ** days_old, min_score)

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
