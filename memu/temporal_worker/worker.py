from __future__ import annotations

import asyncio
import logging

from memu.temporal_contracts import load_temporal_settings, temporal_tls_enabled

logger = logging.getLogger(__name__)

try:
    from temporalio.client import Client
    from temporalio.worker import Worker
except Exception:  # pragma: no cover - worker runtime requires temporalio
    Client = None
    Worker = None


async def main():
    settings = load_temporal_settings()
    if Client is None or Worker is None:
        logger.error("temporalio is not installed; Temporal worker cannot start")
        return

    from memu.temporal_worker.activities import (
        generate_embedding,
        log_audit,
        record_task_event,
        record_temporal_checkpoint,
        search_memory,
        store_memory,
    )
    from memu.temporal_worker.workflows import MemoryIngestionWorkflow, MemorySearchWorkflow

    try:
        client = await Client.connect(
            settings.host,
            namespace=settings.namespace,
            tls=temporal_tls_enabled(settings.host),
        )
        logger.info(
            "Connected to Temporal host=%s namespace=%s queue=%s worker_identity=%s",
            settings.host,
            settings.namespace,
            settings.task_queue,
            settings.worker_identity,
        )
    except Exception as exc:
        logger.error("Failed to connect to Temporal: %s", exc)
        return

    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[MemoryIngestionWorkflow, MemorySearchWorkflow],
        activities=[
            store_memory,
            search_memory,
            log_audit,
            generate_embedding,
            record_temporal_checkpoint,
            record_task_event,
        ],
    )

    logger.info(
        "Temporal worker started queue=%s namespace=%s identity=%s",
        settings.task_queue,
        settings.namespace,
        settings.worker_identity,
    )
    await worker.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
