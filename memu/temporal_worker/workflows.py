from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from memu.temporal_contracts import (
    MemoryWorkflowInput,
    SearchWorkflowInput,
    TemporalCheckpointRecord,
    TemporalTaskEventRecord,
)

ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)

CHECKPOINT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=2,
)


async def _record_checkpoint(
    request: MemoryWorkflowInput | SearchWorkflowInput,
    *,
    sequence: int,
    step: str,
    status: str,
    scratchpad: str,
    progress_pct: float,
    tokens_consumed: int = 0,
    metadata: dict | None = None,
):
    orchestration = request.orchestration
    if orchestration is None or orchestration.task_id is None:
        return False
    record = TemporalCheckpointRecord(
        task_id=orchestration.task_id,
        workflow_id=workflow.info().workflow_id,
        gateway_id=orchestration.source_gateway or request.agent_id or "temporal-worker",
        checkpoint_sequence=sequence,
        step=step,
        status=status,
        scratchpad=scratchpad,
        progress_pct=progress_pct,
        tokens_consumed=tokens_consumed,
        metadata={
            "lease_key": orchestration.lease_key,
            **(metadata or {}),
        },
    )
    return await workflow.execute_activity(
        "record_temporal_checkpoint",
        args=[record.model_dump(mode="json")],
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=CHECKPOINT_RETRY,
    )


async def _record_task_event(
    request: MemoryWorkflowInput | SearchWorkflowInput,
    *,
    event_type: str,
    payload: dict,
):
    orchestration = request.orchestration
    if orchestration is None or orchestration.task_id is None:
        return False
    event = TemporalTaskEventRecord(
        task_id=orchestration.task_id,
        workflow_id=workflow.info().workflow_id,
        gateway_id=orchestration.source_gateway or request.agent_id or "temporal-worker",
        event_type=event_type,
        payload={
            "lease_key": orchestration.lease_key,
            **payload,
        },
        parent_event_id=orchestration.parent_event_id,
    )
    return await workflow.execute_activity(
        "record_task_event",
        args=[event.model_dump(mode="json")],
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=CHECKPOINT_RETRY,
    )


@workflow.defn
class MemoryIngestionWorkflow:
    @workflow.run
    async def run(self, request: dict) -> str:
        payload = MemoryWorkflowInput.model_validate(request)
        checkpoint_seq = 0

        try:
            await _record_task_event(
                payload,
                event_type="task_claimed",
                payload={
                    "phase": "workflow_started",
                    "agent_id": payload.agent_id,
                },
            )

            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="accepted",
                status="started",
                scratchpad="Temporal memory ingestion accepted and queued for processing.",
                progress_pct=5.0,
            )

            embedding = await workflow.execute_activity(
                "generate_embedding",
                args=[payload.content],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=ACTIVITY_RETRY,
            )

            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="embedding",
                status="completed",
                scratchpad="Embedding generated for memory payload; ready for durable storage.",
                progress_pct=40.0,
            )

            memory_id = await workflow.execute_activity(
                "store_memory",
                args=[payload.content, payload.agent_id, payload.metadata, embedding],
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=ACTIVITY_RETRY,
            )

            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="persist_memory",
                status="completed",
                scratchpad=f"Memory persisted to memU with id={memory_id}.",
                progress_pct=85.0,
                metadata={"memory_id": memory_id},
            )

            await workflow.execute_activity(
                "log_audit",
                args=[
                    "MEMORY_STORED",
                    payload.agent_id,
                    {
                        "memory_id": memory_id,
                        "workflow_id": workflow.info().workflow_id,
                    },
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=ACTIVITY_RETRY,
            )

            await _record_task_event(
                payload,
                event_type="task_completed",
                payload={
                    "phase": "workflow_completed",
                    "memory_id": memory_id,
                    "agent_id": payload.agent_id,
                },
            )

            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="complete",
                status="completed",
                scratchpad="Temporal memory ingestion completed successfully.",
                progress_pct=100.0,
                metadata={"memory_id": memory_id},
            )
            return memory_id
        except Exception as exc:
            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="failed",
                status="failed",
                scratchpad=f"Temporal memory ingestion failed: {exc}",
                progress_pct=100.0,
            )
            await _record_task_event(
                payload,
                event_type="task_failed",
                payload={
                    "phase": "workflow_failed",
                    "agent_id": payload.agent_id,
                    "error": str(exc),
                },
            )
            raise


@workflow.defn
class MemorySearchWorkflow:
    @workflow.run
    async def run(self, request: dict) -> list[dict]:
        payload = SearchWorkflowInput.model_validate(request)
        checkpoint_seq = 0

        try:
            await _record_task_event(
                payload,
                event_type="task_claimed",
                payload={
                    "phase": "workflow_started",
                    "agent_id": payload.effective_agent_id,
                    "query": payload.query,
                },
            )

            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="accepted",
                status="started",
                scratchpad="Temporal memory search accepted and queued for execution.",
                progress_pct=5.0,
            )

            embedding = await workflow.execute_activity(
                "generate_embedding",
                args=[payload.query],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=ACTIVITY_RETRY,
            )

            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="embedding",
                status="completed",
                scratchpad="Embedding generated for async search query.",
                progress_pct=35.0,
            )

            await workflow.execute_activity(
                "log_audit",
                args=[
                    "SEARCH_INTENT",
                    payload.effective_agent_id,
                    {
                        "query": payload.query,
                        "workflow_id": workflow.info().workflow_id,
                    },
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=ACTIVITY_RETRY,
            )

            results = await workflow.execute_activity(
                "search_memory",
                args=[payload.model_dump(mode="json"), embedding],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=ACTIVITY_RETRY,
            )

            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="search",
                status="completed",
                scratchpad=f"Async search completed with {len(results)} result(s).",
                progress_pct=85.0,
                metadata={"result_count": len(results)},
            )

            await workflow.execute_activity(
                "log_audit",
                args=[
                    "SEARCH_COMPLETE",
                    payload.effective_agent_id,
                    {
                        "count": len(results),
                        "workflow_id": workflow.info().workflow_id,
                    },
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=ACTIVITY_RETRY,
            )

            await _record_task_event(
                payload,
                event_type="task_completed",
                payload={
                    "phase": "workflow_completed",
                    "agent_id": payload.effective_agent_id,
                    "result_count": len(results),
                },
            )

            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="complete",
                status="completed",
                scratchpad="Temporal search workflow completed successfully.",
                progress_pct=100.0,
                metadata={"result_count": len(results)},
            )
            return results
        except Exception as exc:
            checkpoint_seq += 1
            await _record_checkpoint(
                payload,
                sequence=checkpoint_seq,
                step="failed",
                status="failed",
                scratchpad=f"Temporal search workflow failed: {exc}",
                progress_pct=100.0,
            )
            await _record_task_event(
                payload,
                event_type="task_failed",
                payload={
                    "phase": "workflow_failed",
                    "agent_id": payload.effective_agent_id,
                    "error": str(exc),
                    "query": payload.query,
                },
            )
            raise
