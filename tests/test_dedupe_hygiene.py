from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memu.decay import compute_final_score, should_deduplicate


def test_should_deduplicate_respects_threshold_boundary():
    assert should_deduplicate(0.95) is True
    assert should_deduplicate(0.9499) is False


def test_compute_final_score_prefers_newer_memory_when_all_else_equal():
    newer = compute_final_score(
        similarity=0.8,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        access_count=1,
    )
    older = compute_final_score(
        similarity=0.8,
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
        access_count=1,
    )

    assert newer > older
