from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from memu.api import _apply_retrieval_safety_policy, _quality_confidence_penalty


def _row(*, row_id: str, metadata: dict, confidence: float = 1.0):
    now = datetime.now(timezone.utc)
    return {
        "id": row_id,
        "metadata": metadata,
        "confidence": confidence,
        "updated_at": now,
    }


def test_retrieval_policy_filters_untrusted_expired_and_superseded_rows():
    kept_id = str(uuid4())
    superseded_id = str(uuid4())

    rows = [
        _row(row_id=kept_id, metadata={"quality": {"confidence": "medium", "expires": None}}),
        _row(row_id=str(uuid4()), metadata={"source": "untrusted", "quality": {"expires": None}}),
        _row(
            row_id=str(uuid4()),
            metadata={"quality": {"expires": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}},
        ),
        _row(row_id=superseded_id, metadata={"quality": {"expires": None}}),
        _row(
            row_id=str(uuid4()),
            metadata={"quality": {"supersedes": superseded_id, "expires": None}},
        ),
    ]

    filtered = _apply_retrieval_safety_policy(rows)
    filtered_ids = {row["id"] for row in filtered}

    assert kept_id in filtered_ids
    assert superseded_id not in filtered_ids
    assert len(filtered_ids) == 2


def test_low_confidence_tag_has_penalty():
    assert _quality_confidence_penalty({"quality": {"confidence": "low"}}) < 1.0
    assert _quality_confidence_penalty({"quality": {"confidence": "high"}}) == 1.0
