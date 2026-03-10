# memu/models.py
from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from enum import Enum

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

class MemoryCreate(BaseModel):
    content: str
    memory_type: MemoryType = MemoryType.observation
    agent_id: str
    metadata: dict | None = None
    parent_id: UUID | None = None
    confidence: float = 1.0
    tags: list[str] = Field(default_factory=list, description="Entity/topic tags for filtered retrieval")
    custom_id: str | None = Field(default=None, description="Caller-supplied idempotency key for dedupe-safe writes")
    allow_hygiene_bypass: bool = Field(default=False, description="Allow write even if content looks like secrets/history noise")

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
    tags: list[str] = Field(default_factory=list)
    custom_id: str | None = None
    duplicate_count: int = 0
    created_at: datetime
    updated_at: datetime

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    agent_id: str | None = None
    memory_type: MemoryType | None = None
    min_confidence: float = 0.0
    temporal_weight: float = 0.3
    tags: list[str] = Field(default_factory=list, description="Filter results to memories containing ALL of these tags")
    search_strategy: str = Field(default="hybrid", description="hybrid | vector | text")
    graph_depth: int = Field(default=1, ge=0, le=2, description="How many graph hops to use for temporal graph boosting")


class MemoryBlockCreate(BaseModel):
    """Create or update a context block."""
    key: str = Field(..., description="Block key, e.g. 'project:jiraflow:status'")
    content: str
    agent_owner: str | None = None
    metadata: dict | None = None


class MemoryBlock(BaseModel):
    """A stable context block for agent state."""
    key: str
    content: str
    agent_owner: str | None
    metadata: dict
    version: int
    created_at: datetime
    updated_at: datetime

class SearchResult(BaseModel):
    memory: Memory
    similarity: float
    final_score: float

class TaskStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    done = "done"
    blocked = "blocked"

class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"

class TaskCreate(BaseModel):
    task: str
    priority: Priority
    owner_id: str
    lane: str | None = None
    metadata: dict | None = None
    dependency_id: UUID | None = None

class Task(BaseModel):
    id: UUID
    task: str
    priority: str
    status: str
    owner_id: str
    lane: str | None
    metadata: dict
    evidence: str | None
    dependency_id: UUID | None
    created_at: datetime
    updated_at: datetime


class TaskUpdate(BaseModel):
    status: TaskStatus | None = None
    evidence: str | None = Field(default=None, description="Verification artifact, log snippet, or proof URL/text")
    metadata: dict | None = None

class BulkImportRequest(BaseModel):
    content: str
    split_on: str = "\n---\n"
    memory_type: MemoryType = MemoryType.observation
    agent_id: str
    custom_ids: list[str] | None = Field(default=None, description="Optional per-chunk custom ids for idempotent bulk imports")
    allow_hygiene_bypass: bool = False

class BulkImportResponse(BaseModel):
    imported: int
    duplicates_skipped: int
    deduplicated_updated: int = 0

class ChatRequest(BaseModel):
    question: str
    agent_id: str | None = None
    context_limit: int = 5

class ChatResponse(BaseModel):
    answer: str
    sources: list[Memory]
