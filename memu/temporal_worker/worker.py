# memu/temporal_worker/worker.py
"""Temporal worker with Progressive Skill Loading.

Activities are lazy-loaded to reduce context window size and memory footprint.

Author: Lenny
"""
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
    gdpr_delete_kv_memory,
    gdpr_delete_vector_memory,
    gdpr_delete_graph_memory,
)
from memu.temporal_worker.workflows import (
    MemoryIngestionWorkflow,
    MemorySearchWorkflow,
    GDPRScrubWorkflow,
)
from memu.skill_loader import get_skill_registry

logger = logging.getLogger(__name__)

async def main():
    # Initialize skill registry (lazy-loading, doesn't load skills yet)
    skill_registry = get_skill_registry()
    logger.info(f"Skill registry initialized. Available skills: {skill_registry.list_available_skills()}")

    # Connect to Temporal server
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    use_tls = os.environ.get("TEMPORAL_TLS", "").lower() == "true" or "443" in host
    try:
        client = await Client.connect(host, tls=use_tls)
        logger.info(f"Connected to Temporal at {host} (tls={use_tls})")
    except Exception as e:
        logger.error(f"Failed to connect to Temporal: {e}")
        return

    # Run the worker with lazy-loaded activities
    # Activities are only loaded into memory when first invoked
    worker = Worker(
        client,
        task_queue="memu-queue",
        workflows=[MemoryIngestionWorkflow, MemorySearchWorkflow, GDPRScrubWorkflow],
        activities=[
            store_memory,
            search_memory,
            log_audit,
            generate_embedding,
            gdpr_delete_kv_memory,
            gdpr_delete_vector_memory,
            gdpr_delete_graph_memory,
        ],
    )

    logger.info("Worker started with progressive skill loading. Listening on 'memu-queue'...")
    await worker.run()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())


