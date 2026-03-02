# memu/temporal_worker/worker.py
import asyncio
import os
import logging
from temporalio.client import Client
from temporalio.worker import Worker
from memu.temporal_worker.activities import (
    store_memory,
    search_memory,
    log_audit,
    generate_embedding,
)
from memu.temporal_worker.workflows import MemoryIngestionWorkflow, MemorySearchWorkflow

logger = logging.getLogger(__name__)

async def main():
    # Connect to Temporal server
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    try:
        client = await Client.connect(host)
        logger.info(f"Connected to Temporal at {host}")
    except Exception as e:
        logger.error(f"Failed to connect to Temporal: {e}")
        return

    # Run the worker
    worker = Worker(
        client,
        task_queue="memu-queue",
        workflows=[MemoryIngestionWorkflow, MemorySearchWorkflow],
        activities=[
            store_memory,
            search_memory,
            log_audit,
            generate_embedding
        ],
    )
    
    logger.info("Worker started. Listening on 'memu-queue'...")
    await worker.run()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())


