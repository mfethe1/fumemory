"""Pydantic models for memU."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


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

class TaskStatus(str, enum.Enum):
    TODO = "todo"
    IN_PROGRESS = "in-progress"
    DONE = "done"
    BLOCKED = "blocked"

class TaskPriority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

class TaskCreate(BaseModel):
    task: str
    priority: TaskPriority = TaskPriority.P2
    owner_id: str | None = None
    lane: str = "general"
    metadata: dict[str, Any] = Field(default_factory=dict)
    dependency_id: UUID | None = None

class Task(BaseModel):
    id: UUID
    task: str
    priority: TaskPriority
    status: TaskStatus
    owner_id: str | None
    lane: str
    metadata: dict[str, Any]
    evidence: str | None
    dependency_id: UUID | None
    created_at: datetime
    updated_at: datetime
