from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from enum import Enum
from typing import Any, Optional

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

class Relationship(BaseModel):
    """Graph-Lite entity/relationship tag for memory connections."""
    entity: str = Field(..., description="Entity name or ID being referenced")
    relationship_type: str = Field(..., description="Type of relationship (e.g., 'similar', 'extends', 'caused_by', 'related')")
    target_memory_id: Optional[UUID] = Field(None, description="Optional: UUID of target memory if known")
    strength: float = Field(0.5, ge=0.0, le=1.0, description="Relationship strength (0.0-1.0)")

class MemoryCreate(BaseModel):
    content: str
    memory_type: MemoryType = MemoryType.observation
    agent_id: str
    metadata: Optional[dict] = None
    parent_id: Optional[UUID] = None
    confidence: float = 1.0
    relationships: list[Relationship] = Field(default_factory=list, description="Graph-Lite entity/relationship tags")

class Memory(BaseModel):
    id: UUID
    content: str
    memory_type: str
    agent_id: str
    metadata: dict
    parent_id: Optional[UUID]
    confidence: float
    access_count: int
    decay_score: Optional[float] = None
    created_at: datetime
    updated_at: datetime

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    agent_id: Optional[str] = None
    memory_type: Optional[MemoryType] = None
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
    owner_id: Optional[str] = None
    lane: Optional[str] = None
    metadata: Optional[dict] = None
    dependency_id: Optional[UUID] = None

    # Extended registry metadata
    risk_score: int = Field(25, ge=0, le=100)
    source: Optional[str] = None
    source_ref: Optional[str] = None
    project: Optional[str] = None
    completion_criteria: Optional[str] = None
    menu_bucket: Optional[str] = None

class Task(BaseModel):
    id: UUID
    task: str
    priority: str
    status: str
    owner_id: Optional[str]
    lane: Optional[str]
    metadata: dict
    evidence: Optional[str]
    dependency_id: Optional[UUID]

    # Extended registry fields
    risk_score: int
    source: Optional[str]
    source_ref: Optional[str]
    project: Optional[str]
    completion_criteria: Optional[str]
    review_status: str
    reviewer_id: Optional[str]
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]
    retry_count: int
    refine_status: str
    refined_at: Optional[datetime]
    source_fingerprint: Optional[str]
    menu_bucket: Optional[str]

    created_at: datetime
    updated_at: datetime

class TaskUpdate(BaseModel):
    status: Optional[TaskStatus] = None
    owner_id: Optional[str] = None
    lane: Optional[str] = None
    priority: Optional[Priority] = None
    risk_score: Optional[int] = Field(None, ge=0, le=100)
    completion_criteria: Optional[str] = None
    review_status: Optional[TaskReviewStatus] = None
    reviewer_id: Optional[str] = None
    metadata: Optional[dict] = None
    evidence: Optional[str] = None

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
    agent_id: Optional[str] = None
    context_limit: int = 5

class ChatResponse(BaseModel):
    answer: str
    sources: list[Memory]


class GatewayLeaseAcquireRequest(BaseModel):
    lease_key: str = Field(min_length=1, max_length=255)
    gateway_id: str = Field(min_length=1, max_length=128)
    backup_gateway: Optional[str] = Field(default=None, max_length=128)
    ttl_seconds: int = Field(default=120, ge=5, le=3600)
    last_message_id: Optional[str] = None
    context_digest: Optional[str] = None
    task_state: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


class GatewayLeaseRenewRequest(BaseModel):
    lease_key: str = Field(min_length=1, max_length=255)
    gateway_id: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=120, ge=5, le=3600)
    last_message_id: Optional[str] = None
    last_reply_id: Optional[str] = None
    context_digest: Optional[str] = None
    task_state: Optional[dict[str, Any]] = None
    metadata: Optional[dict[str, Any]] = None


class GatewayLeaseReleaseRequest(BaseModel):
    lease_key: str = Field(min_length=1, max_length=255)
    gateway_id: str = Field(min_length=1, max_length=128)


class GatewayLease(BaseModel):
    lease_key: str
    owner_gateway: str
    backup_gateway: Optional[str] = None
    lease_expires_at: datetime
    last_message_id: Optional[str] = None
    last_reply_id: Optional[str] = None
    context_digest: Optional[str] = None
    task_state: dict[str, Any]
    metadata: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime


class GatewayLeaseAcquireResponse(BaseModel):
    ok: bool = True
    status: str
    lease: GatewayLease
