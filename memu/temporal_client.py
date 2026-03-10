from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _workflow_suffix(*parts: str) -> str:
    digest = hashlib.sha256("::".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


async def get_client() -> Any:
    try:
        from temporalio.client import Client
    except ModuleNotFoundError as exc:
        raise RuntimeError("temporalio is not installed") from exc

    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    use_tls = os.environ.get("TEMPORAL_TLS", "").lower() == "true" or ":443" in host
    return await Client.connect(host, tls=use_tls)


async def store_memory_workflow(content: str, agent_id: str, metadata: dict | None) -> str | None:
    try:
        from memu.temporal_worker.workflows import MemoryIngestionWorkflow

        client = await get_client()
        wf_id = f"memory-ingest-{agent_id}-{_workflow_suffix(agent_id, content)}"
        handle = await client.start_workflow(
            MemoryIngestionWorkflow.run,
            args=[content, agent_id, metadata or {}],
            id=wf_id,
            task_queue="memu-queue",
        )
        return handle.id
    except Exception as e:
        logger.error("Failed to start store workflow: %s", e)
        return None


async def search_memory_workflow(query: str, agent_id: str) -> list[dict[str, Any]] | None:
    try:
        from memu.temporal_worker.workflows import MemorySearchWorkflow

        client = await get_client()
        wf_id = f"memory-search-{agent_id}-{_workflow_suffix(agent_id, query)}"
        return await client.execute_workflow(
            MemorySearchWorkflow.run,
            args=[query, agent_id],
            id=wf_id,
            task_queue="memu-queue",
        )
    except Exception as e:
        logger.error("Failed to start search workflow: %s", e)
        return None
