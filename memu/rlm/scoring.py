"""Scoring primitives for the RLM retriever.

Ports the combined ranking shape from upstream memU (NevaMind-AI/memU
PR #206): ``similarity × log(reinforcement+1) × recency_decay``.

This module lands the primitive only. Wiring it into the retriever is
deferred to a follow-up PR so this change stays focused on the storage-
layer reinforcement port.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional


# Half-life of the recency term, expressed in days. A 30-day half-life
# means a node observed 30 days ago contributes half the recency weight
# of one observed today. Kept as a module-level default so tests can
# override while callers outside this module use a single number.
DEFAULT_RECENCY_HALFLIFE_DAYS = 30.0


def recency_decay(
    recency_days: float,
    *,
    halflife_days: float = DEFAULT_RECENCY_HALFLIFE_DAYS,
) -> float:
    """Exponential recency decay ``0.5 ** (age / halflife)``.

    - ``recency_days == 0`` -> 1.0 (brand new, full weight).
    - ``recency_days == halflife_days`` -> 0.5.
    - ``recency_days`` far in the future (negative) is clamped to 0 so
      a clock skew can't amplify rankings.
    """
    if recency_days < 0:
        recency_days = 0.0
    if halflife_days <= 0:
        # Guard against pathological input; treat as "no decay".
        return 1.0
    return 0.5 ** (recency_days / halflife_days)


def reinforcement_boost(reinforcement: int) -> float:
    """``log(reinforcement + 1)`` with a floor of 0 for non-positive counts.

    The +1 offset means an un-reinforced node (count=0) contributes 0,
    so the combined score degenerates to 0 and the caller can fall back
    to the similarity term (see :func:`score_combined`). ``natural log``
    mirrors upstream memU.
    """
    if reinforcement <= 0:
        return 0.0
    return math.log(reinforcement + 1)


def score_combined(
    similarity: float,
    reinforcement: int,
    recency_days: float,
    *,
    halflife_days: float = DEFAULT_RECENCY_HALFLIFE_DAYS,
) -> float:
    """Upstream-style combined score.

    Formula: ``similarity × log(reinforcement + e) × recency_decay``.

    The ``+ e`` shift (rather than upstream's ``+ 1``) keeps the helper
    **strictly monotonic** in ``reinforcement``: ``log(0 + e) = 1`` so
    an un-reinforced node produces a baseline of
    ``similarity × decay``, and each additional reinforcement raises
    the boost further (``log(1 + e) > 1``). This avoids the naive
    ``log(r + 1)`` floor of 0 that would zero out the score for the
    most common case (freshly written, never re-seen).

    Monotonically *increasing* in ``reinforcement`` and monotonically
    *decreasing* in ``recency_days``. Negative ``recency_days`` (clock
    skew, future events) are clamped to 0 so they can't amplify
    rankings.
    """
    decay = recency_decay(recency_days, halflife_days=halflife_days)
    r = max(0, int(reinforcement))
    boost = math.log(r + math.e)
    return float(similarity) * boost * decay


def days_since(ts: Optional[datetime], *, now: Optional[datetime] = None) -> float:
    """Helper to turn a ``WikiNode.last_reinforced_at`` into days-ago.

    Returns ``+inf`` if ``ts`` is ``None`` so a node that was never
    reinforced still produces a finite score (decay → 0) without special-
    casing the caller. Timezone-naive inputs are assumed UTC.
    """
    if ts is None:
        return math.inf
    now = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    delta = now - ts
    return delta.total_seconds() / 86_400.0
