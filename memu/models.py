from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from enum import Enum
from typing import Any

class MemoryType(str, Enum):
    # Original types (backwards compatibility)
    fact = "fact"
    pattern = "pattern"
    failure = "failure"
    # A-MEM types
    observation = "observation"
    reflection = "reflection"
    plan = "plan"
    goal = "goal"
    decision = "decision"
    lesson = "lesson"
    user_action = "user_action"
    external = "external"
    # Procedural memory — parameterized skill templates (Phase 3)
    procedural = "procedural"

class MemoryCreate(BaseModel):
    content: str
    memory_type: MemoryType = MemoryType.observation
    agent_id: str
    metadata: dict | None = None
    parent_id: UUID | None = None
    confidence: float = 1.0
    salience_score: float = Field(0.5, ge=0.0, le=1.0, description="Salience: 0.0=routine, 1.0=critical")

class Memory(BaseModel):
    id: UUID
    content: str
    memory_type: str
    agent_id: str
    metadata: dict
    parent_id: UUID | None
    confidence: float
    access_count: int
    decay_score: float | None = None
    salience_score: float = 0.5
    searchable: bool = True
    created_at: datetime
    updated_at: datetime

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    agent_id: str | None = None
    memory_type: MemoryType | None = None
    min_confidence: float = 0.0
    temporal_weight: float = 0.3
    min_results: int = 3
    max_expansion_steps: int = 3
    lexical_fallback: bool = True
    # Temporal retrieval lane (Graphiti-inspired)
    time_window_start: datetime | None = None
    time_window_end: datetime | None = None
    entity_weight: float = 0.15

class SearchResult(BaseModel):
    memory: Memory
    similarity: float
    final_score: float

class TaskStatus(str, Enum):
    pending = "pending"
    claimed = "claimed"
    in_progress = "in_progress"
    done = "done"
    blocked = "blocked"
    cancelled = "cancelled"

class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

class TaskReviewStatus(str, Enum):
    pending_review = "pending_review"
    passed = "passed"
    needs_input = "needs_input"
    blocked = "blocked"
    completed = "completed"

class RefineStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    needs_revision = "needs_revision"

class TaskCreate(BaseModel):
    task: str
    priority: Priority
    owner_id: str | None = None
    lane: str | None = None
    metadata: dict | None = None
    dependency_id: UUID | None = None

    # Extended registry metadata
    risk_score: int = Field(25, ge=0, le=100)
    source: str | None = None
    source_ref: str | None = None
    project: str | None = None
    completion_criteria: str | None = None
    menu_bucket: str | None = None

class Task(BaseModel):
    id: UUID
    task: str
    priority: str
    status: str
    owner_id: str | None
    lane: str | None
    metadata: dict
    evidence: str | None
    dependency_id: UUID | None

    # Extended registry fields
    risk_score: int
    source: str | None
    source_ref: str | None
    project: str | None
    completion_criteria: str | None
    review_status: str
    reviewer_id: str | None
    reviewed_at: datetime | None
    review_notes: str | None
    retry_count: int
    refine_status: str
    refined_at: datetime | None
    source_fingerprint: str | None
    menu_bucket: str | None

    created_at: datetime
    updated_at: datetime

class TaskUpdate(BaseModel):
    status: TaskStatus | None = None
    owner_id: str | None = None
    lane: str | None = None
    priority: Priority | None = None
    risk_score: int | None = Field(None, ge=0, le=100)
    completion_criteria: str | None = None
    review_status: TaskReviewStatus | None = None
    reviewer_id: str | None = None
    metadata: dict | None = None
    evidence: str | None = None

class TaskReviewResult(str, Enum):
    approve = "approve"
    needs_info = "needs_info"
    rework = "rework"
    block = "block"

class TaskReviewRequest(BaseModel):
    reviewer_id: str
    decision: TaskReviewResult
    notes: str

class BulkImportRequest(BaseModel):
    content: str
    split_on: str = "\n---\n"
    memory_type: MemoryType = MemoryType.observation
    agent_id: str

class BulkImportResponse(BaseModel):
    imported: int
    duplicates_skipped: int

class ChatRequest(BaseModel):
    question: str
    agent_id: str | None = None
    context_limit: int = 5

class ChatResponse(BaseModel):
    answer: str
    sources: list[Memory]


class GatewayLeaseAcquireRequest(BaseModel):
    lease_key: str = Field(min_length=1, max_length=255)
    gateway_id: str = Field(min_length=1, max_length=128)
    backup_gateway: str | None = Field(default=None, max_length=128)
    ttl_seconds: int = Field(default=120, ge=5, le=3600)
    last_message_id: str | None = None
    context_digest: str | None = None
    task_state: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class GatewayLeaseRenewRequest(BaseModel):
    lease_key: str = Field(min_length=1, max_length=255)
    gateway_id: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=120, ge=5, le=3600)
    last_message_id: str | None = None
    last_reply_id: str | None = None
    context_digest: str | None = None
    task_state: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class GatewayLeaseReleaseRequest(BaseModel):
    lease_key: str = Field(min_length=1, max_length=255)
    gateway_id: str = Field(min_length=1, max_length=128)


class GatewayLease(BaseModel):
    lease_key: str
    owner_gateway: str
    backup_gateway: str | None = None
    lease_expires_at: datetime
    last_message_id: str | None = None
    last_reply_id: str | None = None
    context_digest: str | None = None
    task_state: dict[str, Any]
    metadata: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class GatewayLeaseAcquireResponse(BaseModel):
    ok: bool = True
    status: str
    lease: GatewayLease
