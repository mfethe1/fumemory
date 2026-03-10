# memu/temporal_client.py
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

try:
    from temporalio.client import Client
except Exception:  # pragma: no cover - optional dependency in local/test environments
    Client = None


async def get_client():
    if Client is None:
        raise RuntimeError("temporalio is not installed")
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    return await Client.connect(host)


async def store_memory_workflow(content: str, agent_id: str, metadata: dict):
    try:
        from memu.temporal_worker.workflows import MemoryIngestionWorkflow

        client = await get_client()
        wf_id = f"memory-ingest-{agent_id}-{abs(hash(content))}"
        handle = await client.start_workflow(
            MemoryIngestionWorkflow.run,
            args=[content, agent_id, metadata],
            id=wf_id,
            task_queue="memu-queue",
        )
        return handle.id
    except Exception as e:
        logger.error(f"Failed to start store workflow: {e}")
        return None


async def search_memory_workflow(query: str, agent_id: str):
    try:
        from memu.temporal_worker.workflows import MemorySearchWorkflow

        client = await get_client()
        wf_id = f"memory-search-{agent_id}-{abs(hash(query))}"
        return await client.execute_workflow(
            MemorySearchWorkflow.run,
            args=[query, agent_id],
            id=wf_id,
            task_queue="memu-queue",
        )
    except Exception as e:
        logger.error(f"Failed to start search workflow: {e}")
        return None
