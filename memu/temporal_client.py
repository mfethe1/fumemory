import os
import logging
from temporalio.client import Client
from memu.temporal_worker.workflows import MemoryIngestionWorkflow, MemorySearchWorkflow

logger = logging.getLogger(__name__)


async def get_client():
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    use_tls = os.environ.get("TEMPORAL_TLS", "").lower() == "true" or ":443" in host
    return await Client.connect(host, tls=use_tls)


async def store_memory_workflow(content: str, agent_id: str, metadata: dict | None):
    try:
        client = await get_client()
        wf_id = f"memory-ingest-{agent_id}-{abs(hash(content))}"
        handle = await client.start_workflow(
            MemoryIngestionWorkflow.run,
            args=[content, agent_id, metadata or {}],
            id=wf_id,
            task_queue="memu-queue",
        )
        return handle.id
    except Exception as e:
        logger.error(f"Failed to start store workflow: {e}")
        return None


async def search_memory_workflow(query: str, agent_id: str):
    try:
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
