"""Manager for shared operational state using NATS KV."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

import nats

from memu.nats_config import primary_nats_url
from memu.swarm_models import GatewayStatus

logger = logging.getLogger(__name__)

NATS_URL = primary_nats_url()

class StateManager:
    def __init__(self, gateway_id: str, nats_url: str = NATS_URL):
        self.gateway_id = gateway_id
        self.nats_url = nats_url
        self.nc = None
        self.js = None
        self.state_kv = None
        self.settings_kv = None
        self._pulse_task = None

    async def connect(self):
        self.nc = await nats.connect(self.nats_url)
        self.js = self.nc.jetstream()
        self.state_kv = await self.js.key_value("GATEWAY_STATE")
        self.settings_kv = await self.js.key_value("GLOBAL_SETTINGS")
        logger.info(f"StateManager connected to {self.nats_url}")

    async def update_state(self, status: GatewayStatus, current_task_id: UUID | None = None, metadata: dict | None = None):
        if not self.state_kv:
            return

        state = {
            "gateway_id": self.gateway_id,
            "status": status.value,
            "current_task_id": str(current_task_id) if current_task_id else None,
            "metadata": metadata or {},
            "last_pulse": datetime.now(timezone.utc).isoformat(),
        }
        await self.state_kv.put(self.gateway_id, json.dumps(state).encode())

    async def start_pulse(self, interval_s: int = 120):
        """Start a background task to pulse state every interval_s."""
        if self._pulse_task:
            return

        async def _pulse_loop():
            while True:
                try:
                    await self.update_state(GatewayStatus.ONLINE)
                except Exception as e:
                    logger.error(f"Failed to pulse state: {e}")
                await asyncio.sleep(interval_s)

        self._pulse_task = asyncio.create_task(_pulse_loop())
        logger.info(f"Started state pulse for {self.gateway_id}")

    async def stop_pulse(self):
        if self._pulse_task:
            self._pulse_task.cancel()
            await asyncio.gather(self._pulse_task, return_exceptions=True)
            self._pulse_task = None

    async def get_gateway_state(self, gateway_id: str) -> dict | None:
        try:
            entry = await self.state_kv.get(gateway_id)
            return json.loads(entry.value.decode())
        except Exception:
            return None

    async def close(self):
        await self.stop_pulse()
        if self.nc:
            await self.nc.close()
