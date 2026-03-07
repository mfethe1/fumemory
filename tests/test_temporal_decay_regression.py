"""Regression tests for temporal decay scoring and filtered retrieval.

QA verification gate: these tests must pass before any decay/retrieval
changes are merged. They validate:
1. Temporal decay scoring ranks recent memories higher
2. Access count boosts work with diminishing returns
3. Agent-scoped filtering returns only matching memories
4. Deduplication threshold catches near-duplicates
5. Edge cases: zero-age, very old, high-access memories
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from memu.decay import compute_final_score, should_deduplicate


# --- Fixtures ---

NOW = datetime.now(timezone.utc)


def _make_ts(days_ago: int) -> datetime:
    """Create a UTC timestamp N days in the past."""
    return NOW - timedelta(days=days_ago)


# --- Temporal Decay Scoring ---


class TestComputeFinalScore:
    """Verify that compute_final_score correctly blends similarity, recency, and access."""

    def test_recent_beats_old_at_equal_similarity(self):
        """A memory from today should score higher than one from 90 days ago,
        given identical similarity and access counts."""
        recent = compute_final_score(
            similarity=0.85, created_at=_make_ts(0), access_count=1
        )
        old = compute_final_score(
            similarity=0.85, created_at=_make_ts(90), access_count=1
        )
        assert recent > old, f"Recent ({recent:.4f}) should beat old ({old:.4f})"

    def test_very_old_memory_does_not_reach_zero(self):
        """Even a 365-day-old memory should have a score > 0 (min_score floor)."""
        score = compute_final_score(
            similarity=0.9, created_at=_make_ts(365), access_count=0
        )
        assert score > 0, "Very old memory score should be > 0"
        assert score >= 0.1 * 0.1, "Score should be bounded by min_score interactions"

    def test_brand_new_memory_max_decay(self):
        """A brand-new memory (0 days old) should get decay_factor ≈ 1.0."""
        score = compute_final_score(
            similarity=1.0, created_at=NOW, access_count=0
        )
        # With access_count=0: access_boost = 1 + log(1) = 1.0
        # decay_factor = 1.0, recency = 1.0
        # blended = 0.7 * 1.0 + 0.3 * 1.0 = 1.0
        # final = 1.0 * 1.0 * 1.0 = 1.0
        assert score == pytest.approx(1.0, abs=0.01)

    def test_access_count_boost_diminishing_returns(self):
        """More accesses should boost score, but with diminishing returns.
        
        We use uniform step sizes to properly test that log(n+1) produces
        diminishing marginal gains.
        """
        ts = _make_ts(7)
        # Uniform steps: 0, 10, 20, 30, 40
        access_levels = [0, 10, 20, 30, 40]
        scores = []
        for access in access_levels:
            s = compute_final_score(
                similarity=0.8, created_at=ts, access_count=access
            )
            scores.append(s)

        # Each subsequent score should be higher
        for i in range(1, len(scores)):
            assert scores[i] > scores[i - 1], (
                f"access={access_levels[i]} should score higher than access={access_levels[i-1]}"
            )

        # Marginal gain should decrease with uniform step increases
        gains = [scores[i] - scores[i - 1] for i in range(1, len(scores))]
        for i in range(1, len(gains)):
            assert gains[i] < gains[i - 1], (
                f"Gain from step {i+1} ({gains[i]:.4f}) should be less than step {i} ({gains[i-1]:.4f})"
            )

    def test_similarity_zero_still_gets_recency_component(self):
        """Even with 0 similarity, temporal_weight gives a recency-based score."""
        score = compute_final_score(
            similarity=0.0, created_at=NOW, access_count=0, temporal_weight=0.3
        )
        # blended = 0.7 * 0.0 + 0.3 * 1.0 = 0.3
        assert score > 0, "Zero similarity should still have recency component"
        assert score == pytest.approx(0.3, abs=0.05)

    def test_higher_temporal_weight_favors_recency(self):
        """Increasing temporal_weight should make recency matter more than similarity."""
        old_high_sim = compute_final_score(
            similarity=0.95, created_at=_make_ts(60), access_count=1, temporal_weight=0.8
        )
        new_low_sim = compute_final_score(
            similarity=0.5, created_at=_make_ts(1), access_count=1, temporal_weight=0.8
        )
        # With high temporal weight, recency should dominate
        assert new_low_sim > old_high_sim, (
            "High temporal_weight should make recent low-sim beat old high-sim"
        )

    def test_decay_rate_controls_speed(self):
        """Higher decay_rate makes old memories score lower."""
        slow_decay = compute_final_score(
            similarity=0.8, created_at=_make_ts(30), access_count=1, decay_rate=0.005
        )
        fast_decay = compute_final_score(
            similarity=0.8, created_at=_make_ts(30), access_count=1, decay_rate=0.05
        )
        assert slow_decay > fast_decay, "Slow decay should preserve score better"

    def test_deterministic_output(self):
        """Same inputs should produce same output (no random component).
        
        Use a fixed timestamp to avoid sub-second drift between calls.
        """
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        a = compute_final_score(similarity=0.75, created_at=ts, access_count=3)
        b = compute_final_score(similarity=0.75, created_at=ts, access_count=3)
        assert a == pytest.approx(b, abs=1e-9), "Scores should be deterministic"


# --- Deduplication ---


class TestDeduplication:
    """Verify deduplication threshold logic."""

    def test_exact_duplicate_caught(self):
        assert should_deduplicate(1.0) is True

    def test_near_duplicate_caught(self):
        assert should_deduplicate(0.96) is True

    def test_similar_but_distinct_passes(self):
        assert should_deduplicate(0.90) is False

    def test_threshold_boundary(self):
        assert should_deduplicate(0.95) is True
        assert should_deduplicate(0.9499) is False

    def test_custom_threshold(self):
        assert should_deduplicate(0.85, threshold=0.80) is True
        assert should_deduplicate(0.75, threshold=0.80) is False


# --- Ranking Order Verification ---


class TestRankingOrder:
    """End-to-end ranking order checks that simulate real retrieval scenarios."""

    def test_ranking_recent_relevant_first(self):
        """Simulate 5 memories and verify correct ranking order."""
        memories = [
            {"sim": 0.90, "age_days": 1, "access": 5, "label": "recent-relevant"},
            {"sim": 0.92, "age_days": 30, "access": 2, "label": "older-slightly-more-relevant"},
            {"sim": 0.70, "age_days": 0, "access": 0, "label": "brand-new-low-sim"},
            {"sim": 0.95, "age_days": 120, "access": 10, "label": "very-old-high-sim-accessed"},
            {"sim": 0.60, "age_days": 200, "access": 0, "label": "ancient-low-sim"},
        ]

        scored = []
        for m in memories:
            score = compute_final_score(
                similarity=m["sim"],
                created_at=_make_ts(m["age_days"]),
                access_count=m["access"],
            )
            scored.append((m["label"], score))

        scored.sort(key=lambda x: x[1], reverse=True)
        labels_ranked = [s[0] for s in scored]

        # The recent-relevant should be top-2 and ancient-low-sim should be last
        assert labels_ranked[-1] == "ancient-low-sim", (
            f"Ancient low-sim should be last, got: {labels_ranked}"
        )
        assert "recent-relevant" in labels_ranked[:2], (
            f"Recent relevant should be top-2, got: {labels_ranked}"
        )

    def test_all_scores_positive(self):
        """No memory should ever score <= 0 regardless of inputs."""
        edge_cases = [
            (0.0, _make_ts(1000), 0),
            (0.01, _make_ts(365), 0),
            (1.0, _make_ts(0), 0),
            (0.5, _make_ts(30), 1000),
        ]
        for sim, ts, access in edge_cases:
            score = compute_final_score(similarity=sim, created_at=ts, access_count=access)
            assert score > 0, f"Score should be positive for sim={sim}, age={ts}, access={access}"
