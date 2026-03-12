"""NATS Routing for Parallel Sub-Agent Spawning.

This module implements the routing logic to spawn and coordinate parallel sub-agents
for broken-out tasks. It handles worker discovery, task distribution, result aggregation,
and failure recovery.

Key Features:
- Dynamic worker pool management
- Load-balanced task distribution
- Parallel execution with result aggregation
- Automatic retry and failure recovery
- Integration with task orchestrator and supply chain
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from nats.aio.client import Client as NATS
from pydantic import BaseModel

from memu.cluster import NATSClusterManager
from memu.swarm_models import EventType, SwarmEvent
from memu.worker_payloads import (
    SpawnParallelBatchPayload,
    SpawnSubtaskPayload,
    WorkerClaimPayload,
    WorkerErrorPayload,
    WorkerHeartbeatPayload,
    WorkerMessage,
    WorkerMessageType,
    WorkerReadyPayload,
    WorkerResultPayload,
)

logger = logging.getLogger(__name__)


class WorkerInfo(BaseModel):
    """Information about an available worker."""
    
    worker_id: str
    gateway_id: str
    supported_capabilities: list[str]
    max_concurrent_tasks: int
    current_task_count: int
    available_memory_mb: int
    available_cpu_cores: float
    last_heartbeat: datetime
    ready_for_work: bool


class ParallelWorkerRouter:
    """Routes tasks to parallel workers and aggregates results."""
    
    def __init__(
        self,
        cluster: NATSClusterManager,
        gateway_id: str,
    ):
        self.cluster = cluster
        self.gateway_id = gateway_id
        
        # Worker registry
        self._workers: dict[str, WorkerInfo] = {}
        self._worker_tasks: dict[str, set[UUID]] = {}  # worker_id -> task_ids
        
        # Task tracking
        self._active_batches: dict[UUID, SpawnParallelBatchPayload] = {}
        self._batch_results: dict[UUID, dict[UUID, Any]] = {}  # batch_id -> {task_id -> result}
        self._batch_errors: dict[UUID, dict[UUID, str]] = {}  # batch_id -> {task_id -> error}
        
        # Callbacks
        self._batch_completion_callbacks: dict[UUID, Callable] = {}
    
    async def start(self):
        """Start the router and subscribe to worker messages."""
        nc = self.cluster.active_connection
        
        # Subscribe to worker announcements
        await nc.subscribe("swarm.worker.ready", cb=self._handle_worker_ready)
        await nc.subscribe("swarm.worker.heartbeat", cb=self._handle_worker_heartbeat)
        await nc.subscribe("swarm.worker.result", cb=self._handle_worker_result)
        await nc.subscribe("swarm.worker.error", cb=self._handle_worker_error)
        
        logger.info(f"ParallelWorkerRouter started (gateway={self.gateway_id})")
        
        # Start worker health monitor
        asyncio.create_task(self._monitor_worker_health())
    
    async def _handle_worker_ready(self, msg):
        """Handle worker ready announcement."""
        try:
            payload = WorkerReadyPayload.model_validate_json(msg.data.decode())
            
            worker_info = WorkerInfo(
                worker_id=payload.worker_id,
                gateway_id=payload.gateway_id,
                supported_capabilities=payload.supported_capabilities,
                max_concurrent_tasks=payload.max_concurrent_tasks,
                current_task_count=payload.current_task_count,
                available_memory_mb=payload.available_memory_mb,
                available_cpu_cores=payload.available_cpu_cores,
                last_heartbeat=payload.created_at,
                ready_for_work=payload.ready_for_work,
            )
            
            self._workers[payload.worker_id] = worker_info
            
            logger.info(
                f"Worker registered: {payload.worker_id} "
                f"(capabilities: {len(payload.supported_capabilities)}, "
                f"max_tasks: {payload.max_concurrent_tasks})"
            )
            
        except Exception as e:
            logger.error(f"Failed to handle worker ready: {e}")
    
    async def _handle_worker_heartbeat(self, msg):
        """Handle worker heartbeat."""
        try:
            payload = WorkerHeartbeatPayload.model_validate_json(msg.data.decode())
            
            if payload.worker_id in self._workers:
                self._workers[payload.worker_id].last_heartbeat = payload.heartbeat_at
                
        except Exception as e:
            logger.error(f"Failed to handle worker heartbeat: {e}")
    
    async def _handle_worker_result(self, msg):
        """Handle worker result."""
        try:
            payload = WorkerResultPayload.model_validate_json(msg.data.decode())
            
            # Find which batch this task belongs to
            batch_id = self._find_batch_for_task(payload.task_id)
            if not batch_id:
                logger.warning(f"Received result for unknown task: {payload.task_id}")
                return
            
            # Store result
            if batch_id not in self._batch_results:
                self._batch_results[batch_id] = {}
            
            self._batch_results[batch_id][payload.task_id] = {
                "success": payload.success,
                "result_data": payload.result_data,
                "result_summary": payload.result_summary,
                "execution_time_seconds": payload.execution_time_seconds,
                "tokens_used": payload.tokens_used,
                "worker_id": payload.worker_id,
            }
            
            # Update worker task count
            if payload.worker_id in self._worker_tasks:
                self._worker_tasks[payload.worker_id].discard(payload.task_id)
            
            if payload.worker_id in self._workers:
                self._workers[payload.worker_id].current_task_count -= 1
            
            logger.info(
                f"Task {payload.task_id} completed by {payload.worker_id} "
                f"(success={payload.success})"
            )
            
            # Check if batch is complete
            await self._check_batch_completion(batch_id)
            
        except Exception as e:
            logger.error(f"Failed to handle worker result: {e}")

    async def _handle_worker_error(self, msg):
        """Handle worker error."""
        try:
            payload = WorkerErrorPayload.model_validate_json(msg.data.decode())

            # Find which batch this task belongs to
            batch_id = self._find_batch_for_task(payload.task_id)
            if not batch_id:
                logger.warning(f"Received error for unknown task: {payload.task_id}")
                return

            # Store error
            if batch_id not in self._batch_errors:
                self._batch_errors[batch_id] = {}

            self._batch_errors[batch_id][payload.task_id] = payload.error_message

            # Update worker task count
            if payload.worker_id in self._worker_tasks:
                self._worker_tasks[payload.worker_id].discard(payload.task_id)

            if payload.worker_id in self._workers:
                self._workers[payload.worker_id].current_task_count -= 1

            logger.error(
                f"Task {payload.task_id} failed on {payload.worker_id}: "
                f"{payload.error_message}"
            )

            # Retry if appropriate
            batch = self._active_batches.get(batch_id)
            if batch and payload.is_retryable and payload.retry_count < 3:
                logger.info(f"Retrying task {payload.task_id} (attempt {payload.retry_count + 1})")
                # Find the original subtask and retry
                for subtask in batch.subtasks:
                    if subtask.subtask_id == payload.task_id:
                        await asyncio.sleep(payload.suggested_retry_delay_seconds)
                        await self._dispatch_subtask(subtask, batch_id)
                        break
            else:
                # Check if batch should fail
                await self._check_batch_completion(batch_id)

        except Exception as e:
            logger.error(f"Failed to handle worker error: {e}")

    def _find_batch_for_task(self, task_id: UUID) -> UUID | None:
        """Find which batch a task belongs to."""
        for batch_id, batch in self._active_batches.items():
            for subtask in batch.subtasks:
                if subtask.subtask_id == task_id:
                    return batch_id
        return None

    async def spawn_parallel_batch(
        self,
        batch: SpawnParallelBatchPayload,
        on_complete: Callable[[UUID, dict[UUID, Any]], None] | None = None,
    ) -> UUID:
        """Spawn a batch of parallel subtasks."""
        batch_id = batch.batch_id

        # Register batch
        self._active_batches[batch_id] = batch
        self._batch_results[batch_id] = {}
        self._batch_errors[batch_id] = {}

        if on_complete:
            self._batch_completion_callbacks[batch_id] = on_complete

        logger.info(
            f"Spawning parallel batch {batch_id}: {len(batch.subtasks)} subtasks "
            f"(mode={batch.execution_mode}, max_parallel={batch.max_parallel_workers})"
        )

        # Dispatch subtasks
        if batch.execution_mode == "parallel":
            # Dispatch all at once, respecting max_parallel_workers
            semaphore = asyncio.Semaphore(batch.max_parallel_workers)

            async def dispatch_with_limit(subtask):
                async with semaphore:
                    await self._dispatch_subtask(subtask, batch_id)

            await asyncio.gather(*[
                dispatch_with_limit(subtask) for subtask in batch.subtasks
            ])

        elif batch.execution_mode == "sequential":
            # Dispatch one at a time
            for subtask in batch.subtasks:
                await self._dispatch_subtask(subtask, batch_id)
                # Wait for completion before next
                while subtask.subtask_id not in self._batch_results.get(batch_id, {}):
                    await asyncio.sleep(0.5)

        else:  # adaptive
            # Start with parallel, adjust based on results
            await self._adaptive_dispatch(batch, batch_id)

        return batch_id

    async def _dispatch_subtask(self, subtask: SpawnSubtaskPayload, batch_id: UUID):
        """Dispatch a single subtask to an available worker."""
        # Find best worker
        worker = self._find_best_worker(subtask)

        if not worker:
            logger.warning(f"No available worker for subtask {subtask.subtask_id}, queuing...")
            # TODO: Implement task queue for when no workers available
            return

        # Track assignment
        if worker.worker_id not in self._worker_tasks:
            self._worker_tasks[worker.worker_id] = set()
        self._worker_tasks[worker.worker_id].add(subtask.subtask_id)

        # Update worker state
        worker.current_task_count += 1

        # Publish task to worker
        nc = self.cluster.active_connection

        message = WorkerMessage(
            message_type=WorkerMessageType.SPAWN_SUBTASK,
            source_gateway=self.gateway_id,
            target_gateway=worker.gateway_id,
            payload=subtask.model_dump(),
        )

        await nc.publish(
            f"swarm.worker.{worker.worker_id}.tasks",
            message.model_dump_json().encode()
        )

        logger.info(
            f"Dispatched subtask {subtask.subtask_id} to worker {worker.worker_id}"
        )

    def _find_best_worker(self, subtask: SpawnSubtaskPayload) -> WorkerInfo | None:
        """Find the best available worker for a subtask."""
        candidates = []

        for worker in self._workers.values():
            if not worker.ready_for_work:
                continue

            if worker.current_task_count >= worker.max_concurrent_tasks:
                continue

            # Check capability match
            has_capabilities = all(
                cap in worker.supported_capabilities
                for cap in subtask.required_capabilities
            )
            if not has_capabilities:
                continue

            # Check resource availability
            if worker.available_memory_mb < subtask.max_memory_mb:
                continue

            candidates.append(worker)

        if not candidates:
            return None

        # Sort by current load (least loaded first)
        candidates.sort(key=lambda w: w.current_task_count / w.max_concurrent_tasks)

        return candidates[0]

    async def _check_batch_completion(self, batch_id: UUID):
        """Check if a batch is complete and trigger callback."""
        batch = self._active_batches.get(batch_id)
        if not batch:
            return

        results = self._batch_results.get(batch_id, {})
        errors = self._batch_errors.get(batch_id, {})

        total_tasks = len(batch.subtasks)
        completed_tasks = len(results) + len(errors)

        if completed_tasks < total_tasks:
            return  # Not complete yet

        logger.info(
            f"Batch {batch_id} complete: {len(results)} succeeded, {len(errors)} failed"
        )

        # Check if batch should be considered successful
        success = True
        if batch.require_all_success and errors:
            success = False

        # Aggregate results based on strategy
        aggregated_result = self._aggregate_results(batch, results, errors)

        # Trigger callback
        if batch_id in self._batch_completion_callbacks:
            callback = self._batch_completion_callbacks[batch_id]
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(batch_id, aggregated_result)
                else:
                    callback(batch_id, aggregated_result)
            except Exception as e:
                logger.error(f"Batch completion callback failed: {e}")

        # Cleanup
        del self._active_batches[batch_id]
        del self._batch_results[batch_id]
        del self._batch_errors[batch_id]
        if batch_id in self._batch_completion_callbacks:
            del self._batch_completion_callbacks[batch_id]

    def _aggregate_results(
        self,
        batch: SpawnParallelBatchPayload,
        results: dict[UUID, Any],
        errors: dict[UUID, str],
    ) -> dict[str, Any]:
        """Aggregate results based on batch strategy."""
        strategy = batch.result_aggregation_strategy

        if strategy == "collect_all":
            return {
                "success": len(errors) == 0,
                "results": results,
                "errors": errors,
                "total_tasks": len(batch.subtasks),
                "successful_tasks": len(results),
                "failed_tasks": len(errors),
            }

        elif strategy == "first_success":
            # Return first successful result
            for task_id, result in results.items():
                if result.get("success"):
                    return {
                        "success": True,
                        "result": result,
                        "task_id": str(task_id),
                    }
            return {
                "success": False,
                "error": "No successful results",
                "errors": errors,
            }

        elif strategy == "majority_vote":
            # Count successful vs failed
            success_count = len(results)
            fail_count = len(errors)

            return {
                "success": success_count > fail_count,
                "success_count": success_count,
                "fail_count": fail_count,
                "results": results,
                "errors": errors,
            }

        else:
            return {"error": f"Unknown aggregation strategy: {strategy}"}

    async def _adaptive_dispatch(self, batch: SpawnParallelBatchPayload, batch_id: UUID):
        """Adaptively dispatch tasks based on worker performance."""
        # Start with a small batch
        initial_batch_size = min(3, len(batch.subtasks))

        for i, subtask in enumerate(batch.subtasks):
            if i < initial_batch_size:
                # Dispatch initial batch in parallel
                await self._dispatch_subtask(subtask, batch_id)
            else:
                # Wait for at least one to complete before dispatching next
                while len(self._batch_results.get(batch_id, {})) < i - initial_batch_size + 1:
                    await asyncio.sleep(0.5)

                await self._dispatch_subtask(subtask, batch_id)

    async def _monitor_worker_health(self):
        """Monitor worker health and remove stale workers."""
        while True:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds

                now = datetime.now(timezone.utc)
                stale_workers = []

                for worker_id, worker in self._workers.items():
                    # Remove workers that haven't sent heartbeat in 60 seconds
                    if (now - worker.last_heartbeat).total_seconds() > 60:
                        stale_workers.append(worker_id)

                for worker_id in stale_workers:
                    logger.warning(f"Removing stale worker: {worker_id}")
                    del self._workers[worker_id]
                    if worker_id in self._worker_tasks:
                        del self._worker_tasks[worker_id]

            except Exception as e:
                logger.error(f"Worker health monitor error: {e}")

    def get_worker_stats(self) -> dict[str, Any]:
        """Get current worker pool statistics."""
        total_workers = len(self._workers)
        ready_workers = sum(1 for w in self._workers.values() if w.ready_for_work)
        total_capacity = sum(w.max_concurrent_tasks for w in self._workers.values())
        current_load = sum(w.current_task_count for w in self._workers.values())

        return {
            "total_workers": total_workers,
            "ready_workers": ready_workers,
            "total_capacity": total_capacity,
            "current_load": current_load,
            "utilization": current_load / total_capacity if total_capacity > 0 else 0,
            "active_batches": len(self._active_batches),
        }
