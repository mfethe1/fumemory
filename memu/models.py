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
    # Procedural memory — parameterized skill templates (Phase 3)
    procedural = "procedural"


class MemoryKind(str, Enum):
    """Primary discriminator between immutable execution proof and reusable knowledge.

    evidence — append-only, task-bound execution proof written by OpenClaw gateways.
    learning — derived, reusable knowledge distilled from one or more evidence records.

    Distinct from memory_type, which is a semantic taxonomy (decision, lesson, etc.).
    """
    evidence = "evidence"
    learning = "learning"


class ReviewStatus(str, Enum):
    """Lifecycle state for Learning Memory, tracking reflection review progress."""
    proposed = "proposed"
    accepted = "accepted"
    accepted_by_timeout = "accepted_by_timeout"
    rejected = "rejected"
    legacy = "legacy"

class Relationship(BaseModel):
    """Graph-Lite entity/relationship tag for memory connections."""
    entity: str = Field(..., description="Entity name or ID being referenced")
    relationship_type: str = Field(..., description="Type of relationship (e.g., 'similar', 'extends', 'caused_by', 'related')")
    target_memory_id: Optional[UUID] = Field(None, description="Optional: UUID of target memory if known")
    strength: float = Field(0.5, ge=0.0, le=1.0, description="Relationship strength (0.0-1.0)")

class MemoryCreate(BaseModel):
    content: str
    memory_type: MemoryType = MemoryType.observation
    memory_kind: MemoryKind = MemoryKind.learning
    agent_id: str
    metadata: Optional[dict] = None
    parent_id: Optional[UUID] = None
    confidence: float = 1.0
    relationships: list[Relationship] = Field(default_factory=list, description="Graph-Lite entity/relationship tags")
    supersedes: UUID | None = None
    invalidates: list[UUID] = Field(default_factory=list)
    salience_score: float = Field(0.5, ge=0.0, le=1.0, description="Salience: 0.0=routine, 1.0=critical")
    allowed_roles: list[str] = Field(default_factory=lambda: ["*"], description="ABAC: roles that may access this memory. ['*'] = unrestricted.")
    idempotency_key: Optional[str] = Field(
        None,
        max_length=255,
        description=(
            "Tenant-scoped idempotency key for Evidence Memory. "
            "Exact replay returns the same ID; same key with a different canonical "
            "payload returns 409 Conflict."
        ),
    )

class Memory(BaseModel):
    id: UUID
    content: str
    memory_type: str
    memory_kind: str = "learning"
    agent_id: str
    metadata: dict
    parent_id: Optional[UUID]
    confidence: float
    access_count: int
    decay_score: Optional[float] = None
    salience_score: float = 0.5
    searchable: bool = True
    review_status: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class RecallMode(str, Enum):
    """Explicit recall mode. learning is the default; forensic must be requested."""
    learning = "learning"
    forensic = "forensic"


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
    agent_roles: list[str] | None = Field(None, description="ABAC: caller's roles for access filtering. None = no filtering.")


class ForensicRecallRequest(BaseModel):
    """Request shape for Forensic Recall — returns Evidence Memory with replay-grade provenance."""
    query: Optional[str] = Field(None, description="Optional semantic/lexical query to narrow evidence records.")
    task_id: Optional[str] = Field(None, description="Filter by OpenClaw task ID stored in metadata.")
    session_id: Optional[str] = Field(None, description="Filter by session ID stored in metadata.")
    gateway_id: Optional[str] = Field(None, description="Filter by gateway ID stored in metadata.")
    agent_id: Optional[str] = Field(None, description="Filter by agent_id column.")
    event_type: Optional[str] = Field(None, description="Filter by event_type stored in metadata.")
    time_window_start: Optional[datetime] = Field(None, description="Earliest created_at to include.")
    time_window_end: Optional[datetime] = Field(None, description="Latest created_at to include.")
    artifact_ref: Optional[str] = Field(None, description="Filter evidence whose artifact_refs contain this value.")
    limit: int = Field(20, ge=1, le=200)
    cursor: Optional[str] = Field(None, description="Opaque pagination cursor from a previous response.")
    include_content: bool = Field(True, description="When False, content is redacted from all items.")
    agent_roles: Optional[list[str]] = Field(None, description="ABAC: caller roles for per-record access filtering.")


class ForensicRecallItem(BaseModel):
    """Single evidence record returned by Forensic Recall with replay-grade provenance."""
    evidence_id: UUID
    memory_kind: str
    memory_type: str
    content: Optional[str] = None
    redacted: bool = False
    redaction_reason: Optional[str] = None
    event_type: Optional[str] = None
    event_at: Optional[datetime] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    gateway_id: Optional[str] = None
    agent_id: str
    source: Optional[str] = None
    source_ref: Optional[str] = None
    artifact_refs: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=lambda: ["*"])
    provenance_links: list[str] = Field(
        default_factory=list,
        description="Source evidence IDs for learning records or correction record IDs for evidence.",
    )
    created_at: datetime


class ForensicRecallResponse(BaseModel):
    """Paginated response for Forensic Recall."""
    items: list[ForensicRecallItem]
    next_cursor: Optional[str] = None
    total_count: Optional[int] = None

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


# ---------------------------------------------------------------------------
# Reflection Review Queue models (Issue #28)
# ---------------------------------------------------------------------------

class ReflectionSource(str, Enum):
    """Origin of a reflection proposal."""
    task_close = "task_close"
    idle_dream = "idle_dream"


class ReflectionProposalStatus(str, Enum):
    """Lifecycle state for a reflection proposal."""
    pending = "pending"
    accepted = "accepted"
    accepted_by_timeout = "accepted_by_timeout"
    rejected = "rejected"
    superseded = "superseded"


class ReflectionAction(str, Enum):
    """Actions a reviewer can take on a pending proposal."""
    approve = "approve"
    deny = "deny"
    edit = "edit"
    inspect = "inspect"


class ReflectionProposal(BaseModel):
    proposal_id: UUID
    tenant_id: str
    status: str
    source: str
    summary: str
    content: str
    confidence: float
    risk_flags: list[str]
    source_task_id: Optional[str]
    source_session_id: Optional[str]
    source_evidence_ids: list[str]
    expires_at: datetime
    telegram_message_id: Optional[str]
    memory_id: Optional[UUID]
    superseded_by: Optional[UUID]
    agent_id: str
    created_at: datetime
    updated_at: datetime


class ReflectionProposalCreate(BaseModel):
    source: ReflectionSource
    summary: str
    content: str
    confidence: float = Field(0.7, ge=0.0, le=1.0)
    risk_flags: list[str] = Field(default_factory=list)
    source_task_id: Optional[str] = None
    source_session_id: Optional[str] = None
    source_evidence_ids: list[str] = Field(default_factory=list)
    agent_id: str


class ReflectionActionRequest(BaseModel):
    actor: str
    decision: ReflectionAction
    notes: Optional[str] = None
    edited_content: Optional[str] = None


class ReflectionActionResponse(BaseModel):
    proposal_id: UUID
    decision: str
    status: str
    memory_id: Optional[UUID] = None
    supersedes_id: Optional[UUID] = None
