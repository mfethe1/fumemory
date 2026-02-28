# memu/temporal_client.py
import os
import json
import logging
from temporalio.client import Client
from temporalio.worker import Worker
from memu.temporal_worker.workflows import MemoryIngestionWorkflow, MemorySearchWorkflow

logger = logging.getLogger(__name__)

# Use a global client to avoid reconnect overhead
_client = None

async def get_client():
    global _client
    if _client is None:
        try:
            host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
            _client = await Client.connect(host)
        except Exception as e:
            logger.error(f"Failed to connect to Temporal: {e}")
            return None
    return _client

async def store_memory_workflow(content: str, agent_id: str, metadata: dict):
    client = await get_client()
    if not client:
        logger.warning("Temporal unavailable, falling back (not implemented)")
        return None
        
    try:
        # Use content hash to dedupe workflows if needed, but unique IDs are safer for logs
        wf_id = f"memory-ingest-{agent_id}-{abs(hash(content))}"
        return await client.execute_workflow(
            MemoryIngestionWorkflow.run,
            content, agent_id, metadata,
            id=wf_id,
            task_queue="memu-queue",
        )
    except Exception as e:
        logger.error(f"Failed to start workflow: {e}")
        return None

async def search_memory_workflow(query: str, agent_id: str):
    client = await get_client()
    if not client:
        return None

    try:
        wf_id = f"memory-search-{agent_id}-{abs(hash(query))}"
        return await client.execute_workflow(
            MemorySearchWorkflow.run,
            query, agent_id,
            id=wf_id,
            task_queue="memu-queue",
        )
    except Exception as e:
        logger.error(f"Search workflow failed: {e}")
        return None
