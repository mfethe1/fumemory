from __future__ import annotations

import logging

from memu.models import MemoryCreate, SearchRequest
from memu.temporal_contracts import (
    MemoryWorkflowInput,
    SearchWorkflowInput,
    TemporalAcceptedResponse,
    canonical_workflow_id,
    coerce_orchestration_context,
    load_temporal_settings,
    temporal_tls_enabled,
)

logger = logging.getLogger(__name__)

try:
    from temporalio.client import Client
except Exception:  # pragma: no cover - optional dependency in local/test environments
    Client = None


async def get_client():
    if Client is None:
        raise RuntimeError("temporalio is not installed")
    settings = load_temporal_settings()
    return await Client.connect(
        settings.host,
        namespace=settings.namespace,
        tls=temporal_tls_enabled(settings.host),
    )


async def store_memory_workflow(req: MemoryCreate) -> TemporalAcceptedResponse | None:
    try:
        from memu.temporal_worker.workflows import MemoryIngestionWorkflow

        settings = load_temporal_settings()
        orchestration = coerce_orchestration_context(req.orchestration, metadata=req.metadata)
        payload = MemoryWorkflowInput(
            content=req.content,
            agent_id=req.agent_id,
            metadata=req.metadata,
            orchestration=orchestration,
        )
        workflow_id = canonical_workflow_id(
            "memory-ingest",
            payload.agent_id,
            payload.content,
            orchestration.task_id if orchestration else None,
            orchestration.lease_key if orchestration else None,
        )
        client = await get_client()
        handle = await client.start_workflow(
            MemoryIngestionWorkflow.run,
            args=[payload.model_dump(mode="json")],
            id=workflow_id,
            task_queue=settings.task_queue,
        )
        return TemporalAcceptedResponse(
            workflow_id=handle.id,
            task_queue=settings.task_queue,
            namespace=settings.namespace,
        )
    except Exception as exc:
        logger.error("Failed to start store workflow: %s", exc)
        return None


async def search_memory_workflow(req: SearchRequest) -> list[dict] | None:
    try:
        from memu.temporal_worker.workflows import MemorySearchWorkflow

        orchestration = coerce_orchestration_context(req.orchestration)
        payload = SearchWorkflowInput(
            query=req.query,
            limit=req.limit,
            agent_id=req.agent_id,
            memory_type=req.memory_type.value if req.memory_type else None,
            min_confidence=req.min_confidence,
            temporal_weight=req.temporal_weight,
            min_results=req.min_results,
            max_expansion_steps=req.max_expansion_steps,
            lexical_fallback=req.lexical_fallback,
            orchestration=orchestration,
        )
        settings = load_temporal_settings()
        client = await get_client()
        workflow_id = canonical_workflow_id(
            "memory-search",
            payload.effective_agent_id,
            payload.query,
            payload.limit,
            payload.memory_type,
            payload.min_confidence,
            payload.temporal_weight,
            orchestration.task_id if orchestration else None,
            orchestration.lease_key if orchestration else None,
        )
        return await client.execute_workflow(
            MemorySearchWorkflow.run,
            args=[payload.model_dump(mode="json")],
            id=workflow_id,
            task_queue=settings.task_queue,
        )
    except Exception as exc:
        logger.error("Failed to start search workflow: %s", exc)
        return None
