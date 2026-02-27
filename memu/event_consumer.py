"""Standalone event consumer service for production paths.

Runs AgentEventsConsumer as a long-lived process and writes periodic summary
events to the memU API so agent event throughput is visible in shared memory.

Environment variables:
    MEMU_API_URL        memU API base URL (default: http://api:8000)
    MEMU_API_KEY        memU API key (default: memu-dev-key)
    SUMMARY_INTERVAL    Seconds between summary writes to memU (default: 30)
    AGENT_ID            Agent identifier for memU writes (default: event_consumer)
    ENABLE_MEMU_WRITES  Set "false" to disable memU summary writes (default: true)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone

import httpx

from memu.agent_events_consumer import AgentEventsConsumer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("memu.event_consumer")

# --- Config ---
MEMU_API_URL = os.environ.get("MEMU_API_URL", "http://api:8000")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
SUMMARY_INTERVAL = float(os.environ.get("SUMMARY_INTERVAL", "30"))
AGENT_ID = os.environ.get("AGENT_ID", "event_consumer")
ENABLE_MEMU_WRITES = os.environ.get("ENABLE_MEMU_WRITES", "true").lower() == "true"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemUSummaryWriter:
    """Writes periodic summary events to memU via HTTP API."""

    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._validated = False

    async def _post(self, content: str, metadata: dict) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/memories",
                    headers={
                        "X-API-Key": self._api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "content": content,
                        "memory_type": "fact",
                        "agent_id": AGENT_ID,
                        "metadata": metadata,
                        "confidence": 1.0,
                    },
                )
                resp.raise_for_status()
                return True
        except Exception as exc:
            logger.warning("memU write failed: %s", exc)
            return False

    async def validate(self) -> bool:
        """Check memU API health before writing."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._base_url}/health",
                    headers={"X-API-Key": self._api_key},
                )
                resp.raise_for_status()
                self._validated = True
                logger.info("memU API validated at %s", self._base_url)
                return True
        except Exception as exc:
            logger.warning("memU API health check failed: %s — writes disabled", exc)
            self._validated = False
            return False

    async def write_summary(self, metrics: dict) -> bool:
        if not self._validated:
            return False

        content = (
            f"[event_consumer] AGENT_EVENTS summary at {_utcnow()}: "
            f"consumed={metrics.get('consumed_count', 0)}, "
            f"acked={metrics.get('acked_count', 0)}, "
            f"failures={metrics.get('failure_count', 0)}, "
            f"replays={metrics.get('replay_count', 0)}, "
            f"ack_rate={metrics.get('ack_rate', 0):.4f}"
        )
        metadata = {
            "source": "event_consumer",
            "event_type": "AGENT_EVENTS_SUMMARY",
            "ts": _utcnow(),
            **metrics,
        }
        ok = await self._post(content, metadata)
        if ok:
            logger.info("EVIDENCE: %s", content)
        return ok


class EventConsumerService:
    """Long-lived service: runs AgentEventsConsumer + periodic memU summary writes."""

    def __init__(self):
        self._consumer = AgentEventsConsumer()
        self._writer = MemUSummaryWriter(MEMU_API_URL, MEMU_API_KEY) if ENABLE_MEMU_WRITES else None
        self._running = False
        self._summary_task: asyncio.Task | None = None

    async def start(self) -> None:
        logger.info(
            "Starting event_consumer service | interval=%ss | memu_writes=%s | api=%s",
            SUMMARY_INTERVAL,
            ENABLE_MEMU_WRITES,
            MEMU_API_URL,
        )

        await self._consumer.start()

        if self._writer:
            validated = await self._writer.validate()
            if not validated:
                logger.warning(
                    "memU API unreachable at startup — will retry on next summary cycle"
                )

        self._running = True
        self._summary_task = asyncio.create_task(self._summary_loop())

    async def stop(self) -> None:
        logger.info("Stopping event_consumer service")
        self._running = False
        if self._summary_task:
            self._summary_task.cancel()
            try:
                await self._summary_task
            except asyncio.CancelledError:
                pass
        await self._consumer.stop()

    async def _summary_loop(self) -> None:
        while self._running:
            await asyncio.sleep(SUMMARY_INTERVAL)
            try:
                await self._emit_summary()
            except Exception as exc:
                logger.error("Summary loop error: %s", exc)

    async def _emit_summary(self) -> None:
        metrics = self._consumer.metrics.as_dict()

        # Always log locally (evidence gate)
        logger.info(
            "AGENT_EVENTS heartbeat | consumed=%d acked=%d failures=%d replays=%d ack_rate=%.4f",
            metrics["consumed_count"],
            metrics["acked_count"],
            metrics["failure_count"],
            metrics["replay_count"],
            metrics["ack_rate"],
        )

        if not self._writer:
            return

        # Re-validate if not yet validated (handles delayed startup)
        if not self._writer._validated:
            await self._writer.validate()

        await self._writer.write_summary(metrics)


async def main() -> None:
    service = EventConsumerService()
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    await service.start()
    await stop_event.wait()
    await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
