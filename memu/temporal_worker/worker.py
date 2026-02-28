import asyncio
import os
import logging
from temporalio.client import Client
from temporalio.worker import Worker
from memu.temporal_worker.activities import StoreMemoryActivity, SearchMemoryActivity, LogAuditActivity
from memu.temporal_worker.workflows import MemoryIngestionWorkflow, MemorySearchWorkflow

logger = logging.getLogger(__name__)

async def main():
    # Connect to Temporal server
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    client = await Client.connect(host)

    # Run the worker
    worker = Worker(
        client,
        task_queue="memu-queue",
        workflows=[MemoryIngestionWorkflow, MemorySearchWorkflow],
        activities=[StoreMemoryActivity, SearchMemoryActivity, LogAuditActivity],
    )
    
    print("Worker started...")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
