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
    created_at: datetime
    updated_at: datetime

class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    agent_id: str | None = None
    memory_type: MemoryType | None = None
    min_confidence: float = 0.0
    temporal_weight: float = 0.3

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
