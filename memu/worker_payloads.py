"""NATS Worker Payload Structures for Parallel Sub-Agent Spawning.

This module defines strict Pydantic schemas for NATS worker messages that spawn
and coordinate parallel sub-agents. All payloads are validated before publishing
to prevent malformed messages from entering the mesh.

Key Features:
- Type-safe payload definitions for all worker message types
- Validation of task breakdown and parallel execution requests
- Integration with task orchestrator and supply chain tracking
- Support for hierarchical task decomposition
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class WorkerMessageType(str, Enum):
    """All valid worker message types."""
    SPAWN_SUBTASK = "spawn_subtask"
    SPAWN_PARALLEL_BATCH = "spawn_parallel_batch"
    TASK_BREAKDOWN_REQUEST = "task_breakdown_request"
    TASK_BREAKDOWN_RESPONSE = "task_breakdown_response"
    WORKER_READY = "worker_ready"
    WORKER_CLAIM = "worker_claim"
    WORKER_HEARTBEAT = "worker_heartbeat"
    WORKER_RESULT = "worker_result"
    WORKER_ERROR = "worker_error"


class SubAgentCapability(str, Enum):
    """Required capabilities for sub-agent execution."""
    CODE_EXECUTION = "code_execution"
    FILE_SYSTEM_ACCESS = "file_system_access"
    NETWORK_ACCESS = "network_access"
    DATABASE_ACCESS = "database_access"
    EXTERNAL_API = "external_api"
    LONG_RUNNING = "long_running"
    GPU_COMPUTE = "gpu_compute"
    MEMORY_INTENSIVE = "memory_intensive"


class SpawnSubtaskPayload(BaseModel):
    """Payload for spawning a single sub-agent task."""
    
    # Task identification
    subtask_id: UUID = Field(default_factory=uuid4)
    parent_task_id: UUID
    root_task_id: UUID
    
    # Task definition
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    
    # Execution requirements
    required_capabilities: list[SubAgentCapability] = Field(default_factory=list)
    compute_budget: float = Field(default=1.0, ge=0.0)
    priority: int = Field(default=5, ge=0, le=10)
    timeout_seconds: int = Field(default=3600, ge=1)
    
    # Resource constraints
    resource_lanes: list[str] = Field(
        default_factory=list,
        description="Semantic mutexes for blast radius control"
    )
    max_memory_mb: int = Field(default=512, ge=128)
    max_cpu_cores: float = Field(default=1.0, ge=0.1)
    
    # Supply chain integration
    long_lead_items: list[str] = Field(default_factory=list)
    estimated_duration_hours: float | None = None
    dependencies: list[UUID] = Field(default_factory=list)
    
    # Context
    context_summary: str | None = Field(
        None,
        description="Compressed context from parent task"
    )
    checkpoint_state: dict[str, Any] = Field(default_factory=dict)
    
    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by_gateway: str = Field(default="unknown")
    
    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        """Ensure title is not just whitespace."""
        if not v.strip():
            raise ValueError("Title cannot be empty or whitespace")
        return v.strip()


class SpawnParallelBatchPayload(BaseModel):
    """Payload for spawning multiple parallel sub-agents."""
    
    batch_id: UUID = Field(default_factory=uuid4)
    parent_task_id: UUID
    root_task_id: UUID
    
    # Batch configuration
    subtasks: list[SpawnSubtaskPayload] = Field(..., min_length=1, max_length=50)
    execution_mode: str = Field(
        default="parallel",
        description="parallel, sequential, or adaptive"
    )
    
    # Coordination
    require_all_success: bool = Field(
        default=True,
        description="Fail parent if any subtask fails"
    )
    max_parallel_workers: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Max concurrent workers"
    )
    
    # Aggregation
    result_aggregation_strategy: str = Field(
        default="collect_all",
        description="How to combine results: collect_all, first_success, majority_vote"
    )
    
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by_gateway: str = Field(default="unknown")
    
    @field_validator("subtasks")
    @classmethod
    def validate_subtasks(cls, v: list[SpawnSubtaskPayload]) -> list[SpawnSubtaskPayload]:
        """Ensure all subtasks have the same parent."""
        if not v:
            raise ValueError("Must have at least one subtask")
        
        parent_ids = {task.parent_task_id for task in v}
        if len(parent_ids) > 1:
            raise ValueError("All subtasks must have the same parent_task_id")
        
        return v


class TaskBreakdownRequest(BaseModel):
    """Request to break down a complex task into subtasks."""
    
    request_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    task_description: str
    
    # Breakdown hints
    max_subtasks: int = Field(default=10, ge=1, le=50)
    preferred_granularity: str = Field(
        default="medium",
        description="fine, medium, or coarse"
    )
    enable_parallel_execution: bool = Field(default=True)
    
    # Context
    available_capabilities: list[SubAgentCapability] = Field(default_factory=list)
    compute_budget: float = Field(default=10.0, ge=0.0)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskBreakdownResponse(BaseModel):
    """Response containing broken-down subtasks."""

    request_id: UUID
    task_id: UUID

    # Breakdown results
    subtasks: list[SpawnSubtaskPayload]
    execution_plan: str = Field(
        ...,
        description="Natural language explanation of the breakdown strategy"
    )

    # Analysis
    estimated_total_duration_hours: float
    critical_path_hours: float
    parallelization_factor: float = Field(
        default=1.0,
        description="Speedup from parallel execution (1.0 = no speedup)"
    )

    # Supply chain
    identified_long_lead_items: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkerReadyPayload(BaseModel):
    """Worker announces readiness to accept tasks."""

    worker_id: str
    gateway_id: str

    # Capabilities
    supported_capabilities: list[SubAgentCapability]
    max_concurrent_tasks: int = Field(default=1, ge=1)

    # Resources
    available_memory_mb: int
    available_cpu_cores: float

    # Status
    current_task_count: int = Field(default=0, ge=0)
    ready_for_work: bool = Field(default=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkerClaimPayload(BaseModel):
    """Worker claims a task for execution."""

    worker_id: str
    gateway_id: str
    task_id: UUID

    # Claim metadata
    claimed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    estimated_completion: datetime | None = None
    fencing_token: int | None = Field(
        None,
        description="Monotonic token for split-brain prevention"
    )


class WorkerHeartbeatPayload(BaseModel):
    """Worker heartbeat with task progress."""

    worker_id: str
    gateway_id: str
    task_id: UUID

    # Progress
    progress_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    status_message: str = Field(default="")

    # Resource usage
    memory_used_mb: int = Field(default=0, ge=0)
    cpu_percent: float = Field(default=0.0, ge=0.0, le=100.0)

    # Checkpointing
    checkpoint_available: bool = Field(default=False)
    checkpoint_summary: str | None = None

    heartbeat_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkerResultPayload(BaseModel):
    """Worker reports task completion."""

    worker_id: str
    gateway_id: str
    task_id: UUID

    # Result
    success: bool
    result_data: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = Field(default="")

    # Metrics
    execution_time_seconds: float
    tokens_used: int = Field(default=0, ge=0)
    compute_cost: float = Field(default=0.0, ge=0.0)

    # Artifacts
    output_files: list[str] = Field(default_factory=list)
    memory_ids: list[UUID] = Field(
        default_factory=list,
        description="memU memory IDs created during execution"
    )

    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkerErrorPayload(BaseModel):
    """Worker reports task failure."""

    worker_id: str
    gateway_id: str
    task_id: UUID

    # Error details
    error_type: str
    error_message: str
    error_traceback: str | None = None

    # Recovery
    is_retryable: bool = Field(default=True)
    retry_count: int = Field(default=0, ge=0)
    suggested_retry_delay_seconds: int = Field(default=60, ge=0)

    # Context
    checkpoint_available: bool = Field(default=False)
    partial_results: dict[str, Any] = Field(default_factory=dict)

    failed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkerMessage(BaseModel):
    """Universal worker message envelope."""

    message_id: UUID = Field(default_factory=uuid4)
    message_type: WorkerMessageType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Routing
    source_gateway: str
    target_gateway: str | None = Field(
        None,
        description="Specific gateway to route to, or None for broadcast"
    )

    # Payload (one of the above payload types)
    payload: dict[str, Any]

    # Metadata
    correlation_id: UUID | None = Field(
        None,
        description="For request/response correlation"
    )
    reply_to: str | None = Field(
        None,
        description="NATS subject for replies"
    )

    # Signature for validation
    signature: str | None = None

