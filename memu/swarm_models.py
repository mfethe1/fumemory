"""Pydantic models for the Swarm Event Mesh.

Every message on the NATS mesh MUST validate against these schemas.
If an LLM hallucinates a bad format, Pydantic rejects it before it hits the mesh.

These are the strict contracts between gateways. No exceptions.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


# --- Enums ---

class EventType(str, enum.Enum):
    """All valid event types on the mesh."""
    TASK_DRAFTED = "task_drafted"
    TASK_AMENDED = "task_amended"
    TASK_CLAIMED = "task_claimed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    BID_SUBMITTED = "bid_submitted"
    LEASE_GRANTED = "lease_granted"
    LEASE_EXPIRED = "lease_expired"
    AUDIT_PROPOSED = "audit_proposed"
    AUDIT_ACCEPTED = "audit_accepted"
    AUDIT_REJECTED = "audit_rejected"
    CIRCUIT_BREAKER = "circuit_breaker"
    SYSTEM_HALT = "system_halt"
    SYSTEM_OVERRIDE = "system_override"
    HEARTBEAT = "heartbeat"
    # Lane locking & fault tolerance
    LANE_ACQUIRED = "lane_acquired"
    LANE_RELEASED = "lane_released"
    LANE_CONTESTED = "lane_contested"
    TASK_ORPHANED = "task_orphaned"
    TASK_HYDRATED = "task_hydrated"
    CHECKPOINT_SAVED = "checkpoint_saved"
    ROLLBACK_EXECUTED = "rollback_executed"
    DLQ_ENQUEUED = "dlq_enqueued"
    DLQ_DIAGNOSED = "dlq_diagnosed"
    DLQ_HEALED = "dlq_healed"
    RPC_REQUEST = "rpc_request"
    RPC_RESPONSE = "rpc_response"


class TaskStatus(str, enum.Enum):
    """Task lifecycle states."""
    PENDING = "pending"
    BIDDING = "bidding"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    AUDIT_PENDING = "audit_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"       # Gateway died mid-execution
    HYDRATING = "hydrating"     # New gateway recovering from checkpoint
    YIELDING = "yielding"       # Waiting for lane lock
    DLQ = "dlq"                 # Poison pill — in dead letter queue
    ROLLING_BACK = "rolling_back"  # Executing rollback instruction


class GatewayStatus(str, enum.Enum):
    """Gateway health states."""
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"


# --- Core Event Envelope ---

class SwarmEvent(BaseModel):
    """The universal event envelope for the NATS mesh.

    Every message published to swarm.* topics MUST be a SwarmEvent.
    This is the atomic unit of communication between gateways.
    """
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now())
    source_gateway: str = Field(..., max_length=64, description="Gateway that produced this event")
    event_type: EventType
    task_id: UUID
    parent_event_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    context_pointer: UUID | None = Field(None, description="Semantic pointer to memU memory ID")
    compute_cost: float = Field(0.0, ge=0.0, description="Token/dollar cost of this action")
    signature: str | None = Field(None, description="Crypto signature for zero-trust RBAC")


# --- Task Models ---

class TaskDrafted(BaseModel):
    """Published when the Session Coordinator creates a new task node in the DAG."""
    title: str = Field(..., max_length=256)
    description: str | None = None
    root_prompt_id: UUID
    parent_task_id: UUID | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    compute_budget: float = Field(0.0, ge=0.0, description="Max compute for this task")
    priority: int = Field(0, ge=0, le=10, description="0=lowest, 10=critical")
    dag_metadata: dict[str, Any] = Field(default_factory=dict)


class BidSubmitted(BaseModel):
    """Published when a worker gateway bids on a task."""
    task_id: UUID
    gateway_id: str = Field(..., max_length=64)
    capabilities_offered: list[str] = Field(default_factory=list)
    estimated_cost: float = Field(0.0, ge=0.0)
    estimated_duration_ms: int = Field(0, ge=0)
    confidence: float = Field(1.0, ge=0.0, le=1.0)


class LeaseGranted(BaseModel):
    """Published when memU awards a task lease to a gateway."""
    task_id: UUID
    gateway_id: str
    lease_duration_seconds: int = Field(45, ge=5, le=300)
    lease_expires: datetime


class AuditProposed(BaseModel):
    """Published when an auditor gateway proposes an amendment to a task result."""
    task_id: UUID
    auditor_gateway: str
    original_result_event: UUID
    amendment_type: str = Field(..., description="correction | enhancement | rejection")
    json_patch: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = Field(..., max_length=2000)
    alignment_score: float = Field(1.0, ge=0.0, le=1.0,
        description="Semantic alignment with original root prompt (1.0 = perfect)")


class ExecutionResult(BaseModel):
    """Published when a gateway completes (or fails) task execution."""
    task_id: UUID
    gateway_id: str
    status: TaskStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    tokens_used: int = 0
    duration_ms: int = 0
    sandbox_id: str | None = Field(None, description="Ephemeral sandbox container ID if used")


class CircuitBreaker(BaseModel):
    """Published when a task's compute budget is exhausted."""
    task_id: UUID
    budget_limit: float
    budget_spent: float
    reason: str = "compute_budget_exhausted"
    requires_human: bool = True


class SystemHalt(BaseModel):
    """Published when God Mode triggers a HALT across the mesh."""
    root_prompt_id: UUID
    halt_reason: str
    invalidated_tasks: list[UUID] = Field(default_factory=list)
    override_instruction: str | None = None
    initiated_by: str = Field(..., description="user | system | auditor")


# --- Gateway Discovery (MCP) ---

class GatewayCapability(BaseModel):
    """A single MCP capability a gateway advertises."""
    name: str = Field(..., max_length=128)
    version: str = Field("1.0.0", max_length=20)
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    cost_per_call: float = Field(0.0, ge=0.0)


class GatewayAnnounce(BaseModel):
    """Published to swarm.discovery when a gateway boots or capabilities change."""
    gateway_id: str = Field(..., max_length=64)
    capabilities: list[GatewayCapability] = Field(default_factory=list)
    status: GatewayStatus = GatewayStatus.ONLINE
    max_concurrent_tasks: int = Field(5, ge=1)
    model_id: str | None = Field(None, description="Primary LLM model this gateway uses")
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- Heartbeat ---

class HeartbeatPing(BaseModel):
    """Published every 5s by a gateway holding a task lease."""
    gateway_id: str
    task_id: UUID
    progress_pct: float = Field(0.0, ge=0.0, le=100.0)
    status_note: str | None = None


# --- Compute Governor ---

class ComputeBudget(BaseModel):
    """Assigned to a root prompt to cap total swarm compute."""
    root_prompt_id: UUID
    max_tokens: int = Field(50_000, ge=0)
    max_cost_usd: float = Field(1.0, ge=0.0)
    tokens_spent: int = 0
    cost_spent_usd: float = 0.0
    is_frozen: bool = False

    @property
    def tokens_remaining(self) -> int:
        return max(self.max_tokens - self.tokens_spent, 0)

    @property
    def cost_remaining(self) -> float:
        return max(self.max_cost_usd - self.cost_spent_usd, 0.0)

    @property
    def is_exhausted(self) -> bool:
        return self.tokens_remaining <= 0 or self.cost_remaining <= 0.0


# --- DAG Projection ---

class TaskNode(BaseModel):
    """A task as a node in the DAG — reconstructed from event projection.

    Includes lane locking (blast radius), fencing tokens (split-brain prevention),
    rollback instructions (saga pattern), and checkpoint state (hydration on failover).
    """
    task_id: UUID
    root_prompt_id: UUID
    parent_task_id: UUID | None = None
    children: list[UUID] = Field(default_factory=list)
    title: str
    status: TaskStatus = TaskStatus.PENDING
    assigned_gateway: str | None = None
    compute_budget: float = 0.0
    compute_spent: float = 0.0
    events: list[UUID] = Field(default_factory=list, description="Ordered event_ids for this task")
    created_at: datetime | None = None
    completed_at: datetime | None = None

    # --- Lane Locking (Semantic Mutexes) ---
    resource_lanes: list[str] = Field(
        default_factory=list,
        description="Blast radius: resources this task will touch (e.g. 'file:/app/main.py', 'table:users')"
    )
    fencing_token: int | None = Field(
        None,
        description="Monotonically increasing token to prevent split-brain overwrites"
    )

    # --- Fault Tolerance ---
    rollback_instruction: str | None = Field(
        None,
        description="Saga pattern: how to undo partial work if this task fails mid-execution"
    )
    checkpoint_state: str | None = Field(
        None,
        description="Last scratchpad checkpoint for hydration handoff on gateway death"
    )
    checkpoint_timestamp: datetime | None = Field(
        None,
        description="When the last checkpoint was saved"
    )
    failure_count: int = Field(0, ge=0, description="Number of times this task has failed (DLQ at 3)")
    last_error: str | None = Field(None, description="Stack trace / error from last failure")
    required_capabilities: list[str] = Field(
        default_factory=list,
        description="MCP capabilities needed (e.g. 'WASM:Postgres', 'MCP:GitHub')"
    )

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    @property
    def is_blocked(self) -> bool:
        """A task is blocked if it has incomplete parent dependencies."""
        return self.status == TaskStatus.PENDING and self.parent_task_id is not None

    @property
    def is_poison_pill(self) -> bool:
        """A task that has failed 3+ times is a poison pill."""
        return self.failure_count >= 3

    @property
    def has_checkpoint(self) -> bool:
        """Whether this task has recoverable state for hydration."""
        return self.checkpoint_state is not None


# --- Lane Locking ---

class LaneLock(BaseModel):
    """A semantic mutex on a resource lane, backed by NATS JetStream KV."""
    lane_id: str = Field(..., description="Resource path (e.g. 'file:/app/main.py', 'table:users')")
    gateway_id: str = Field(..., max_length=64)
    task_id: UUID
    fencing_token: int = Field(..., ge=0, description="Monotonically increasing — prevents split-brain")
    ttl_seconds: int = Field(10, ge=5, le=60, description="Dead man's switch TTL")
    acquired_at: datetime = Field(default_factory=lambda: datetime.now())
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.now() > self.expires_at


class LaneContested(BaseModel):
    """Published when a gateway tries to acquire a lane that's already locked."""
    lane_id: str
    requesting_gateway: str
    holding_gateway: str
    holding_fencing_token: int
    task_id: UUID


# --- Checkpoint & Hydration ---

class CheckpointSaved(BaseModel):
    """Published when a gateway streams incremental state back to memU."""
    task_id: UUID
    gateway_id: str
    checkpoint_sequence: int = Field(..., ge=0, description="Incrementing checkpoint number")
    scratchpad: str = Field(..., max_length=100_000, description="Serialized agent reasoning state")
    progress_pct: float = Field(0.0, ge=0.0, le=100.0)
    tokens_consumed: int = 0


class TaskOrphaned(BaseModel):
    """Published when a gateway's heartbeat expires and the task needs recovery."""
    task_id: UUID
    dead_gateway: str
    last_checkpoint_seq: int | None = None
    has_recoverable_state: bool = False
    time_since_last_heartbeat_ms: int = 0


class HydrationHandoff(BaseModel):
    """Published when a new gateway picks up an orphaned task from checkpoint."""
    task_id: UUID
    recovering_gateway: str
    dead_gateway: str
    checkpoint_sequence: int
    hydration_prompt: str = Field(
        default="You are recovering a task from a failed node. "
                "Here is their partial reasoning. Validate their logic and complete the task.",
        description="System prompt injected into the recovering agent"
    )


# --- Dead Letter Queue (DLQ) & Self-Healing ---

class DLQEntry(BaseModel):
    """A poison pill task that has failed 3+ times."""
    task_id: UUID
    failure_count: int = Field(..., ge=3)
    failures: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of {gateway_id, error, timestamp, stack_trace} for each failure"
    )
    root_cause_diagnosis: str | None = Field(None, description="Medic gateway's diagnosis")
    proposed_amendment: dict[str, Any] | None = Field(None, description="DAG patch to heal the plan")
    dlq_entered_at: datetime = Field(default_factory=lambda: datetime.now())


class RollbackExecuted(BaseModel):
    """Published when a saga rollback is completed before re-attempting a failed task."""
    task_id: UUID
    gateway_id: str
    rollback_instruction: str
    success: bool
    error: str | None = None
    artifacts_cleaned: list[str] = Field(
        default_factory=list,
        description="List of resources that were rolled back (temp tables, files, etc.)"
    )


# --- Mesh RPC (for edge devices without local tooling) ---

class RPCRequest(BaseModel):
    """Published when a gateway needs remote execution of a capability it lacks."""
    request_id: UUID = Field(default_factory=uuid4)
    requesting_gateway: str
    task_id: UUID
    capability: str = Field(..., description="MCP capability needed (e.g. 'WASM:Python')")
    payload: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(30_000, ge=1000, le=300_000)


class RPCResponse(BaseModel):
    """Published in response to an RPCRequest."""
    request_id: UUID
    responding_gateway: str
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    execution_ms: int = 0
