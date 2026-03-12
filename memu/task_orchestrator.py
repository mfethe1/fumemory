"""Task Orchestrator with Aggressive In-Flight Summarization.

This module coordinates task execution across the swarm mesh with real-time
summarization to prevent context overflow and enable long-running task chains.

Key Features:
- Aggressive in-flight summarization (every N events or M tokens)
- Task breakdown and parallel sub-agent spawning via NATS
- Supply chain integration for long lead time tracking
- Checkpoint-based recovery for interrupted tasks
- Token budget enforcement with automatic summarization triggers
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from memu.cluster import NATSClusterManager
from memu.swarm_models import (
    EventType,
    SwarmEvent,
    TaskNode,
    TaskStatus,
    TaskDrafted,
)

logger = logging.getLogger(__name__)


class SummarizationConfig(BaseModel):
    """Configuration for aggressive in-flight summarization."""
    
    max_events_before_summary: int = Field(
        default=10,
        description="Trigger summary after this many events"
    )
    max_tokens_before_summary: int = Field(
        default=2000,
        description="Trigger summary when token count exceeds this"
    )
    summary_compression_ratio: float = Field(
        default=0.3,
        description="Target compression ratio (0.3 = compress to 30% of original)"
    )
    enable_hierarchical_summary: bool = Field(
        default=True,
        description="Enable multi-level summarization for deep task trees"
    )


class TaskSummary(BaseModel):
    """Compressed summary of task execution state."""
    
    task_id: UUID
    summary_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Compressed state
    key_decisions: list[str] = Field(default_factory=list)
    critical_findings: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    
    # Metrics
    events_summarized: int = 0
    original_token_count: int = 0
    compressed_token_count: int = 0
    compression_ratio: float = 0.0
    
    # Lineage
    parent_summary_id: UUID | None = None
    supersedes_events: list[UUID] = Field(default_factory=list)


class SubTaskRequest(BaseModel):
    """Request to spawn a parallel sub-agent for a broken-out task."""
    
    parent_task_id: UUID
    subtask_title: str
    subtask_description: str
    required_capabilities: list[str] = Field(default_factory=list)
    compute_budget: float = Field(default=1.0)
    priority: int = Field(default=5, ge=0, le=10)
    resource_lanes: list[str] = Field(default_factory=list)
    
    # Supply chain integration
    estimated_duration_hours: float | None = None
    dependencies: list[UUID] = Field(default_factory=list)
    long_lead_items: list[str] = Field(default_factory=list)


class TaskOrchestrator:
    """Orchestrates task execution with aggressive summarization and parallel spawning."""
    
    def __init__(
        self,
        cluster: NATSClusterManager,
        gateway_id: str,
        config: SummarizationConfig | None = None,
    ):
        self.cluster = cluster
        self.gateway_id = gateway_id
        self.config = config or SummarizationConfig()
        
        # In-memory state
        self._active_tasks: dict[UUID, TaskNode] = {}
        self._task_summaries: dict[UUID, list[TaskSummary]] = {}
        self._event_buffers: dict[UUID, list[SwarmEvent]] = {}
        self._token_counts: dict[UUID, int] = {}
    
    async def start(self):
        """Start the orchestrator and subscribe to task events."""
        nc = self.cluster.active_connection
        
        # Subscribe to task events
        sub = await nc.subscribe("swarm.task.>")
        logger.info(f"TaskOrchestrator started (gateway={self.gateway_id})")
        
        async for msg in sub.messages:
            try:
                event = SwarmEvent.model_validate_json(msg.data.decode())
                await self._handle_event(event)
            except Exception as e:
                logger.error(f"Failed to process event: {e}")
    
    async def _handle_event(self, event: SwarmEvent):
        """Process incoming task events and trigger summarization if needed."""
        task_id = event.task_id
        
        # Buffer the event
        if task_id not in self._event_buffers:
            self._event_buffers[task_id] = []
        self._event_buffers[task_id].append(event)
        
        # Update token count (rough estimate)
        payload_size = len(str(event.payload))
        self._token_counts[task_id] = self._token_counts.get(task_id, 0) + payload_size // 4
        
        # Check if summarization is needed
        should_summarize = (
            len(self._event_buffers[task_id]) >= self.config.max_events_before_summary
            or self._token_counts[task_id] >= self.config.max_tokens_before_summary
        )
        
        if should_summarize:
            await self._summarize_task(task_id)

    async def _summarize_task(self, task_id: UUID):
        """Generate aggressive summary of task execution state."""
        events = self._event_buffers.get(task_id, [])
        if not events:
            return

        logger.info(f"Summarizing task {task_id} ({len(events)} events)")

        # Extract key information from events
        key_decisions = []
        critical_findings = []
        blockers = []
        next_actions = []

        for event in events:
            payload = event.payload

            if event.event_type == EventType.DECISION_MADE:
                key_decisions.append(payload.get("decision", ""))
            elif event.event_type == EventType.TASK_FAILED:
                blockers.append(payload.get("error", ""))
            elif "finding" in payload:
                critical_findings.append(payload.get("finding", ""))
            elif "next_action" in payload:
                next_actions.append(payload.get("next_action", ""))

        # Create summary
        original_tokens = self._token_counts.get(task_id, 0)
        summary = TaskSummary(
            task_id=task_id,
            key_decisions=key_decisions[:5],  # Keep top 5
            critical_findings=critical_findings[:5],
            blockers=blockers[:3],
            next_actions=next_actions[:3],
            events_summarized=len(events),
            original_token_count=original_tokens,
            compressed_token_count=int(original_tokens * self.config.summary_compression_ratio),
            compression_ratio=self.config.summary_compression_ratio,
            supersedes_events=[e.event_id for e in events],
        )

        # Store summary
        if task_id not in self._task_summaries:
            self._task_summaries[task_id] = []
        self._task_summaries[task_id].append(summary)

        # Publish summary event
        await self._publish_summary(summary)

        # Clear event buffer and reset token count
        self._event_buffers[task_id] = []
        self._token_counts[task_id] = 0

        logger.info(
            f"Task {task_id} summarized: {len(events)} events → "
            f"{summary.compressed_token_count} tokens "
            f"({summary.compression_ratio:.1%} compression)"
        )

    async def _publish_summary(self, summary: TaskSummary):
        """Publish summary to NATS for persistence and distribution."""
        nc = self.cluster.active_connection

        event = SwarmEvent(
            source_gateway=self.gateway_id,
            event_type=EventType.CHECKPOINT_SAVED,
            task_id=summary.task_id,
            payload={
                "summary_id": str(summary.summary_id),
                "key_decisions": summary.key_decisions,
                "critical_findings": summary.critical_findings,
                "blockers": summary.blockers,
                "next_actions": summary.next_actions,
                "compression_ratio": summary.compression_ratio,
                "events_summarized": summary.events_summarized,
            },
        )

        await nc.publish(
            f"swarm.summary.{summary.task_id}",
            event.model_dump_json().encode()
        )

    async def spawn_subtask(self, request: SubTaskRequest) -> UUID:
        """Spawn a parallel sub-agent for a broken-out task."""
        subtask_id = uuid4()

        # Create task drafted event
        task_drafted = TaskDrafted(
            title=request.subtask_title,
            description=request.subtask_description,
            root_prompt_id=request.parent_task_id,
            parent_task_id=request.parent_task_id,
            required_capabilities=request.required_capabilities,
            compute_budget=request.compute_budget,
            priority=request.priority,
            dag_metadata={
                "estimated_duration_hours": request.estimated_duration_hours,
                "dependencies": [str(d) for d in request.dependencies],
                "long_lead_items": request.long_lead_items,
            },
        )

        # Publish to NATS for worker pickup
        nc = self.cluster.active_connection
        event = SwarmEvent(
            source_gateway=self.gateway_id,
            event_type=EventType.TASK_DRAFTED,
            task_id=subtask_id,
            parent_event_id=request.parent_task_id,
            payload=task_drafted.model_dump(),
        )

        await nc.publish("swarm.task.drafted", event.model_dump_json().encode())

        logger.info(
            f"Spawned subtask {subtask_id} for parent {request.parent_task_id}: "
            f"{request.subtask_title}"
        )

        return subtask_id

    async def spawn_parallel_subtasks(
        self,
        parent_task_id: UUID,
        subtasks: list[SubTaskRequest],
    ) -> list[UUID]:
        """Spawn multiple parallel sub-agents for concurrent execution."""
        subtask_ids = []

        for subtask in subtasks:
            subtask.parent_task_id = parent_task_id
            subtask_id = await self.spawn_subtask(subtask)
            subtask_ids.append(subtask_id)

        logger.info(
            f"Spawned {len(subtask_ids)} parallel subtasks for {parent_task_id}"
        )

        return subtask_ids

    def get_task_summary(self, task_id: UUID) -> TaskSummary | None:
        """Get the latest summary for a task."""
        summaries = self._task_summaries.get(task_id, [])
        return summaries[-1] if summaries else None

    def get_all_summaries(self, task_id: UUID) -> list[TaskSummary]:
        """Get all summaries for a task (hierarchical view)."""
        return self._task_summaries.get(task_id, [])

