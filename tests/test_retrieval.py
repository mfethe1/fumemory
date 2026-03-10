from datetime import datetime, timedelta, timezone

from memu.retrieval import (
    blend_hybrid_score,
    compute_graph_temporal_boost,
    reciprocal_rank_fusion,
)


def test_rrf_rewards_documents_present_in_multiple_rankings():
    fused = reciprocal_rank_fusion(
        [
            [("a", 0.9), ("b", 0.8), ("c", 0.7)],
            [("b", 0.95), ("a", 0.5), ("d", 0.4)],
        ]
    )

    assert fused["a"] > fused["c"]
    assert fused["b"] > fused["d"]
    assert fused["a"] == fused["b"]


def test_graph_temporal_boost_prefers_recent_strong_neighbors():
    now = datetime.now(timezone.utc)
    recent = [{"strength": 0.9, "created_at": now - timedelta(days=1)}]
    stale = [{"strength": 0.9, "created_at": now - timedelta(days=45)}]

    assert compute_graph_temporal_boost("m1", recent, now=now) > compute_graph_temporal_boost("m1", stale, now=now)


def test_blend_hybrid_score_includes_temporal_and_graph_components():
    base = blend_hybrid_score(0.4, 0.2, 0.0)
    boosted = blend_hybrid_score(0.4, 0.2, 0.1)

    assert boosted > base
    assert base > 0.4
