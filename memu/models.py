"""Pydantic models for memU."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class MemoryType(str, enum.Enum):
    """Structured memory taxonomy."""
    FACT = "fact"
    DECISION = "decision"
    LESSON = "lesson"
    PATTERN = "pattern"
    FAILURE = "failure"


class MemoryCreate(BaseModel):
    """Request to create or upsert a memory."""
    content: str = Field(..., min_length=1, max_length=50_000)
    memory_type: MemoryType = MemoryType.FACT
    agent_id: str | None = Field(None, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_id: UUID | None = None
    confidence: float = Field(1.0, ge=0.0, le=1.0)

    @field_validator("metadata")
    @classmethod
    def validate_quality_tags(cls, value: dict[str, Any]) -> dict[str, Any]:
        quality = value.get("quality")
        if quality is None:
            return value
        if not isinstance(quality, dict):
            raise ValueError("metadata.quality must be an object")

        confidence_tag = quality.get("confidence")
        if confidence_tag is not None and confidence_tag not in {"high", "medium", "low"}:
            raise ValueError("metadata.quality.confidence must be one of: high, medium, low")

        supersedes = quality.get("supersedes")
        if supersedes is not None:
            if not isinstance(supersedes, str):
                raise ValueError("metadata.quality.supersedes must be a UUID string or null")
            try:
                UUID(supersedes)
            except ValueError as exc:
                raise ValueError("metadata.quality.supersedes must be a valid UUID") from exc

        expires = quality.get("expires")
        if expires is not None:
            if not isinstance(expires, str):
                raise ValueError("metadata.quality.expires must be an ISO-8601 datetime string or null")
            try:
                datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("metadata.quality.expires must be a valid ISO-8601 datetime") from exc

        return value

    @model_validator(mode="after")
    def enforce_confidence_alignment(self) -> "MemoryCreate":
        quality = self.metadata.get("quality", {})
        tag = quality.get("confidence")
        if tag is None:
            return self

        ranges = {
            "low": (0.0, 0.39),
            "medium": (0.4, 0.79),
            "high": (0.8, 1.0),
        }
        lo, hi = ranges[tag]
        if not (lo <= self.confidence <= hi):
            raise ValueError(
                f"confidence={self.confidence} conflicts with metadata.quality.confidence='{tag}'"
            )
        return self


class Memory(BaseModel):
    """A stored memory."""
    id: UUID
    content: str
    memory_type: MemoryType
    agent_id: str | None
    metadata: dict[str, Any]
    parent_id: UUID | None
    confidence: float
    access_count: int
    decay_score: float
    created_at: datetime
    updated_at: datetime


class SearchRequest(BaseModel):
    """Semantic search request."""
    query: str = Field(..., min_length=1)
    limit: int = Field(10, ge=1, le=100)
    agent_id: str | None = None
    memory_type: MemoryType | None = None
    temporal_weight: float = Field(0.3, ge=0.0, le=1.0)
    min_confidence: float = Field(0.0, ge=0.0, le=1.0)
    time_window_start: datetime | None = None
    time_window_end: datetime | None = None
    entity_weight: float = Field(0.15, ge=0.0, le=1.0)


class SearchResult(BaseModel):
    """A search result with score."""
    memory: Memory
    similarity: float
    final_score: float


class ChatRequest(BaseModel):
    """RAG chat request."""
    question: str = Field(..., min_length=1)
    agent_id: str | None = None
    context_limit: int = Field(10, ge=1, le=50)


class ChatResponse(BaseModel):
    """RAG chat response."""
    answer: str
    sources: list[Memory]


class BulkImportRequest(BaseModel):
    """Bulk import from text/markdown."""
    content: str
    agent_id: str | None = None
    memory_type: MemoryType = MemoryType.FACT
    split_on: str = Field("\n\n", description="Delimiter to split content into memories")


class BulkImportResponse(BaseModel):
    """Result of bulk import."""
    imported: int
    duplicates_skipped: int
