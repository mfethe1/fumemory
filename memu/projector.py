"""Event Projector — JetStream → memU Postgres.

Subscribes to swarm.events and projects MEMORY_WRITTEN, DECISION_MADE, 
and task-related events into memU durable storage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import nats
import httpx

from memu.nats_config import primary_nats_url
from memu.swarm_models import SwarmEvent, EventType

logger = logging.getLogger(__name__)

NATS_URL = primary_nats_url()
MEMU_API_URL = os.environ.get("MEMU_API_URL", "http://localhost:8000")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")

class EventProjector:
    def __init__(self, nats_url: str = NATS_URL, memu_url: str = MEMU_API_URL):
        self.nats_url = nats_url
        self.memu_url = memu_url
        self.nc = None
        self.js = None
        self.http_client = httpx.AsyncClient(base_url=memu_url, headers={"X-API-Key": MEMU_API_KEY})

    async def connect(self):
        self.nc = await nats.connect(self.nats_url)
        self.js = self.nc.jetstream()
        logger.info(f"Projector connected to {self.nats_url}")

    async def run(self):
        # Pull-based durable consumer for reliable projection
        sub = await self.js.pull_subscribe("swarm.events", durable="memu-projector")
        logger.info("Projector pull subscription active on swarm.events")

        while True:
            try:
                msgs = await sub.fetch(batch=10, timeout=5)
                for msg in msgs:
                    try:
                        event_data = json.loads(msg.data.decode())
                        event = SwarmEvent.model_validate(event_data)
                        await self.process_event(event)
                        await msg.ack()
                    except Exception as e:
                        logger.error(f"Failed to project event: {e}")
            except nats.errors.TimeoutError:
                # No messages available, loop back
                pass

    async def process_event(self, event: SwarmEvent):
        logger.info(f"Projecting event: {event.event_type} ({event.event_id})")
        
        if event.event_type == EventType.MEMORY_WRITTEN:
            await self._project_memory(event.payload)
        elif event.event_type == EventType.DECISION_MADE:
            await self._project_decision(event.payload)
        elif event.event_type in [EventType.TASK_COMPLETED, EventType.TASK_FAILED]:
            await self._project_task_outcome(event)

    async def _project_memory(self, payload: dict):
        # Deduplicated upsert into memU via API
        try:
            r = await self.http_client.post("/memories", json=payload)
            r.raise_for_status()
            logger.info(f"Memory projected to memU: {r.json().get('id')}")
        except Exception as e:
            logger.error(f"Memory projection failed: {e}")

    async def _project_decision(self, payload: dict):
        # Decisions are also stored as memories of type 'decision'
        try:
            content = f"Decision: {payload['decision']}\nContext: {payload['context']}\nRationale: {payload['rationale']}"
            memory_payload = {
                "content": content,
                "memory_type": "decision",
                "agent_id": payload["agent_id"],
                "confidence": 1.0,
                "metadata": {"options": payload.get("options"), "selected": payload.get("selected_option")}
            }
            r = await self.http_client.post("/memories", json=memory_payload)
            r.raise_for_status()
            logger.info(f"Decision projected to memU: {r.json().get('id')}")
        except Exception as e:
            logger.error(f"Decision projection failed: {e}")

    async def _project_task_outcome(self, event: SwarmEvent):
        # Update task status in memU if tracking tasks there
        pass

    async def close(self):
        await self.http_client.aclose()
        if self.nc:
            await self.nc.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    async def main():
        projector = EventProjector()
        await projector.connect()
        try:
            await projector.run()
        finally:
            await projector.close()

    asyncio.run(main())
