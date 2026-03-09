# memu/nats_worker.py

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Any

from nats.aio.client import Client as NATS

from memu.notion_bridge import NotionBridge

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("memu.worker")

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_TASK_BOARD_ID = os.environ.get("NOTION_TASK_BOARD_ID", "")
MEMU_BASE_URL = os.environ.get("MEMU_BASE_URL", "http://localhost:8000")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "")


async def process_msg(msg: Any, bridge: NotionBridge) -> None:
    """Process a NATS task event and update the Notion bridge.

    Supported subjects:
    - swarm.task.started -> claim_task(task_id, agent_id)
    - swarm.task.completed -> complete_task(task_id, agent_id, notes)
    - swarm.task.failed -> block_task(task_id, agent_id, error)
    """
    subject = msg.subject
    try:
        payload = json.loads(msg.data.decode())
    except Exception:
        logger.error("Invalid JSON payload on subject %s", subject)
        if hasattr(msg, "nak"):
            await msg.nak()
        return

    task_id = payload.get("task_id")
    agent_id = payload.get("agent_id")
    if not task_id or not agent_id:
        logger.error("Missing task_id or agent_id on subject %s", subject)
        if hasattr(msg, "nak"):
            await msg.nak()
        return

    if subject == "swarm.task.started":
        await bridge.claim_task(task_id, agent_id)
    elif subject == "swarm.task.completed":
        await bridge.complete_task(task_id, agent_id, payload.get("notes", ""))
    elif subject == "swarm.task.failed":
        await bridge.block_task(task_id, agent_id, payload.get("error", "Unknown error"))
    else:
        logger.info("Ignoring unsupported subject %s", subject)

    if hasattr(msg, "ack"):
        await msg.ack()


async def run() -> None:
    nc = NATS()
    bridge = NotionBridge(
        notion_token=NOTION_API_KEY,
        memu_base_url=MEMU_BASE_URL,
        memu_api_key=MEMU_API_KEY,
        task_board_id=NOTION_TASK_BOARD_ID,
    )

    try:
        await nc.connect(servers=[NATS_URL])
        logger.info("Connected to NATS at %s", NATS_URL)
    except Exception as e:
        logger.error("Failed to connect to NATS: %s", e)
        await bridge.close()
        return

    async def message_handler(msg: Any) -> None:
        logger.info("Received message on '%s'", msg.subject)
        try:
            await process_msg(msg, bridge)
        except Exception as e:
            logger.exception("Worker message handling failed: %s", e)
            if hasattr(msg, "nak"):
                await msg.nak()

    await nc.subscribe("swarm.task.started", cb=message_handler)
    await nc.subscribe("swarm.task.completed", cb=message_handler)
    await nc.subscribe("swarm.task.failed", cb=message_handler)

    logger.info("Listening for swarm task messages...")

    stop_event = asyncio.Event()

    def signal_handler() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
    except NotImplementedError:
        # Windows fallback in non-main-thread contexts.
        pass

    await stop_event.wait()

    await nc.drain()
    await nc.close()
    await bridge.close()
    logger.info("Worker stopped.")


if __name__ == "__main__":
    asyncio.run(run())
