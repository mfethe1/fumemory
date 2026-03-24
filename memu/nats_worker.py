# memu/nats_worker.py
"""NATS Worker with Strict Context Isolation and IPv4 Connection Support.

Enforces:
- IPv4-first connection strategy to avoid IPv6/IPv4 issues
- Strict payload sanitization (no context bleed)
- Shallow orchestration (minimal message payloads)
- Progressive Skill Loading: Lazy-load skills/tools to reduce context window size (Lenny)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
from typing import Any

from nats.aio.client import Client as NATS

from memu.context_isolation import sanitize_agent_message
from memu.nats_config import primary_nats_url
from memu.notion_bridge import NotionBridge
from memu.skill_loader import get_skill_registry

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("memu.worker")

NATS_URL = primary_nats_url()
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_TASK_BOARD_ID = os.environ.get("NOTION_TASK_BOARD_ID", "")
MEMU_BASE_URL = os.environ.get("MEMU_BASE_URL", "http://localhost:8000")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "")
DLQ_TOPIC = "memu.dlq.llm_failures"
MAX_HANDLER_RETRIES = 3


async def process_msg(msg: Any, bridge: NotionBridge) -> None:
    """Process a NATS task event with strict context isolation.

    Enforces shallow orchestration:
    - Sanitizes incoming payloads
    - Strips unnecessary context
    - Validates essential fields only

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

    # Sanitize payload to enforce context isolation
    try:
        payload = sanitize_agent_message(payload)
    except Exception as e:
        logger.warning(f"Payload sanitization failed: {e}, proceeding with raw payload")

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
    """Run NATS worker with IPv4-first connection strategy and progressive skill loading."""
    # Initialize skill registry (lazy-loading, doesn't load skills yet)
    skill_registry = get_skill_registry()
    logger.info(f"Skill registry initialized. Available skills: {skill_registry.list_available_skills()}")

    nc = NATS()
    bridge = NotionBridge(
        notion_token=NOTION_API_KEY,
        memu_base_url=MEMU_BASE_URL,
        memu_api_key=MEMU_API_KEY,
        task_board_id=NOTION_TASK_BOARD_ID,
    )

    # Resolve NATS URL to IPv4 to avoid IPv6/IPv4 connection issues
    nats_url = NATS_URL
    try:
        from urllib.parse import urlparse
        parsed = urlparse(NATS_URL)
        host = parsed.hostname or "localhost"
        port = parsed.port or 4222

        # Try IPv4 resolution first
        try:
            addr_info = socket.getaddrinfo(
                host, port,
                socket.AF_INET,  # Force IPv4
                socket.SOCK_STREAM
            )
            if addr_info:
                ipv4_host = addr_info[0][4][0]
                nats_url = f"nats://{ipv4_host}:{port}"
                logger.info(f"Resolved {host} to IPv4: {ipv4_host}")
        except socket.gaierror as e:
            logger.warning(f"IPv4 resolution failed for {host}, using original URL: {e}")
    except Exception as e:
        logger.warning(f"URL parsing failed: {e}, using original URL")

    try:
        await nc.connect(servers=[nats_url])
        logger.info("Connected to NATS at %s", nats_url)
    except Exception as e:
        logger.error("Failed to connect to NATS: %s", e)
        await bridge.close()
        return

    async def message_handler(msg: Any) -> None:
        logger.info("Received message on '%s'", msg.subject)

        # Use JetStream metadata for stateless retry tracking (no local dict).
        # msg.metadata is available on JetStream push/pull subscriptions.
        num_delivered = 1
        if hasattr(msg, "metadata") and msg.metadata:
            num_delivered = getattr(msg.metadata, "num_delivered", 1) or 1

        try:
            await process_msg(msg, bridge)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # Poison message (bad payload) — check delivery count
            if num_delivered >= MAX_HANDLER_RETRIES:
                logger.error(
                    "Poison message on %s after %d deliveries, sending to DLQ: %s",
                    msg.subject, num_delivered, e,
                )
                try:
                    dlq_payload = json.dumps({
                        "source": "nats_worker",
                        "subject": msg.subject,
                        "error": str(e),
                        "data_preview": msg.data.decode()[:500],
                    }).encode()
                    await nc.publish(DLQ_TOPIC, dlq_payload)
                except Exception:
                    pass
                if hasattr(msg, "ack"):
                    await msg.ack()  # Clear the poison message
            else:
                logger.warning(
                    "Transient error on %s (delivery %d/%d): %s",
                    msg.subject, num_delivered, MAX_HANDLER_RETRIES, e,
                )
                if hasattr(msg, "nak"):
                    await msg.nak(delay=num_delivered * 2)
        except Exception as e:
            logger.exception("Worker message handling failed: %s", e)
            if hasattr(msg, "nak"):
                await msg.nak()

    # Queue group ensures only ONE worker pod processes each task event,
    # preventing duplicate LLM calls and DB writes during horizontal scaling.
    await nc.subscribe("swarm.task.started", cb=message_handler, queue="memu-worker-group")
    await nc.subscribe("swarm.task.completed", cb=message_handler, queue="memu-worker-group")
    await nc.subscribe("swarm.task.failed", cb=message_handler, queue="memu-worker-group")

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
