from __future__ import annotations

import pytest
from pydantic import ValidationError

from memu.models import MemoryCreate


def test_memory_create_accepts_valid_quality_tags():
    req = MemoryCreate(
        content="Store this memory",
        confidence=0.9,
        metadata={
            "quality": {
                "confidence": "high",
                "supersedes": "11111111-1111-1111-1111-111111111111",
                "expires": "2026-03-01T00:00:00Z",
            }
        },
    )
    assert req.metadata["quality"]["confidence"] == "high"


def test_memory_create_rejects_invalid_supersedes():
    with pytest.raises(ValidationError):
        MemoryCreate(
            content="bad supersedes",
            confidence=0.9,
            metadata={"quality": {"confidence": "high", "supersedes": "not-a-uuid", "expires": None}},
        )


def test_memory_create_rejects_invalid_expires():
    with pytest.raises(ValidationError):
        MemoryCreate(
            content="bad expires",
            confidence=0.9,
            metadata={
                "quality": {
                    "confidence": "high",
                    "supersedes": "11111111-1111-1111-1111-111111111111",
                    "expires": "not-a-date",
                }
            },
        )


def test_memory_create_rejects_confidence_tag_mismatch():
    with pytest.raises(ValidationError):
        MemoryCreate(
            content="tag mismatch",
            confidence=0.2,
            metadata={
                "quality": {
                    "confidence": "high",
                    "supersedes": None,
                    "expires": None,
                }
            },
        )
