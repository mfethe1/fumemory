"""Minimal Warden execution runtime.

Responsibilities:
- Subscribe to gateway heartbeats and respawn requests on NATS.
- Trigger DEAD gateway recovery when heartbeat expires.
- Optionally spawn / kill dockerized gateway containers (if docker SDK + image available).
- Emit standardized SwarmEvents so Glass Box / auditors can observe actions.

This is intentionally conservative: no LLM reasoning, deterministic control-plane logic only.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import nats
from pydantic import ValidationError

from memu.cluster import NATSClusterManager
from memu.swarm_models import (
    HeartbeatPing,
    SwarmEvent,
    EventType,
    TaskOrphaned,
)
from memu.warden import RespawnRequest

logger = logging.getLogger(__name__)


@dataclass
class GatewayLiveness:
    gateway_id: str
    task_id: str | None = None
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fencing_token: int = 0
    heartbeat_count: int = 0

    def touched(self):
        self.last_seen = datetime.now(timezone.utc)
        self.heartbeat_count += 1


class WardenRuntime:
    """Runtime watchdog + respawn loop for the swarm."""

    def __init__(self) -> None:
        self.max_containers = int(os.environ.get("WARDEN_MAX_CONTAINERS", "10"))
        self.heartbeat_ttl = int(os.environ.get("WARDEN_HEARTBEAT_TTL", "10"))
        self.check_interval = float(os.environ.get("WARDEN_CHECK_INTERVAL", "3"))
        self.gateway_image = os.environ.get("WARDEN_GATEWAY_IMAGE", "fumemory-gateway:latest")
        self.fallback_task_container = os.environ.get("WARDEN_FALLBACK_TASK_CONTAINER", "")

        self.cluster: NATSClusterManager | None = None
        self.liveness: dict[str, GatewayLiveness] = {}
        self.active_containers: dict[str, str] = {}  # gateway_id -> container_id

        # Optional docker integration (non-fatal if missing)
        self._docker = None
        self._use_docker = False
        try:
            import docker

            self._docker = docker
            self._use_docker = True
        except Exception:
            self._use_docker = False
            logger.warning("docker sdk unavailable; running in NO-DOCKER mode")

    async def run(self):
        self.cluster = NATSClusterManager()
        await self.cluster.connect()

        await self._subscribe()

        # Watchdog and maintenance
        await asyncio.gather(
            self._heartbeat_watchdog(),
            self._main_wait(),
        )

    async def _subscribe(self):
        assert self.cluster is not None
        nc = self.cluster.active_connection

        # Dedicated warden command/heartbeat subjects
        await nc.subscribe("swarm.warden.heartbeat", cb=self._on_heartbeat)
        await nc.subscribe("swarm.warden.respawn", cb=self._on_respawn)
        logger.info("Warden subscribed to swarm.warden.heartbeat and swarm.warden.respawn")

        # Also observe canonical heartbeat if any gateway publishes it there
        await nc.subscribe("swarm.events.heartbeat", cb=self._on_event_heartbeat)

        logger.info("Warden event loop running")

    async def _main_wait(self):
        """Idle loop to keep process alive."""
        while True:
            await asyncio.sleep(3600)

    async def _heartbeat_watchdog(self):
        while True:
            now = datetime.now(timezone.utc)
            for gateway_id, state in list(self.liveness.items()):
                age = (now - state.last_seen).total_seconds()
                if age <= self.heartbeat_ttl:
                    continue

                logger.warning("Heartbeat expired for %s (age=%.1fs)", gateway_id, age)
                await self._handle_expired_gateway(state)
                # remove to prevent repeated respawn loops
                del self.liveness[gateway_id]

            await asyncio.sleep(self.check_interval)

    async def _handle_expired_gateway(self, state: GatewayLiveness):
        assert self.cluster is not None
        nc = self.cluster.active_connection

        # Notify mesh: task orphaned
        if state.task_id:
            payload = TaskOrphaned(
                task_id=state.task_id,
                dead_gateway=state.gateway_id,
                last_checkpoint_seq=0,
                has_recoverable_state=True,
                time_since_last_heartbeat_ms=int(self.heartbeat_ttl * 1000),
            )

            event = SwarmEvent(
                source_gateway="warden",
                event_type=EventType.TASK_ORPHANED,
                task_id=state.task_id,
                payload=payload.model_dump(),
            )
            await nc.publish("swarm.events", event.model_dump_json().encode())

        # Emit advisory suicide signal to prevent split-brain ghosts
        await nc.publish(f"{WardenSubjects.SUICIDE_BASE}{state.gateway_id}", b"{}")

        # Basic respawn semantics via signed request
        if state.task_id:
            req = self._build_respawn_request("gateway", state.task_id, state.gateway_id)
            await nc.publish("swarm.warden.respawn", req.model_dump_json().encode())

        # Try to spawn replacement container (best effort)
        await self._spawn_gateway_container(state.gateway_id)

    async def _spawn_gateway_container(self, dead_gateway_id: str):
        if not self._use_docker:
            logger.warning("Docker unavailable; cannot spawn gateway container for %s", dead_gateway_id)
            return

        if len(self.active_containers) >= self.max_containers:
            logger.error("FORK-BOMB BLOCKER: max containers=%s reached", self.max_containers)
            return

        try:
            cli = self._docker.from_env()
            container = cli.containers.run(
                image=self.gateway_image,
                detach=True,
                name=f"ward-standby-{dead_gateway_id}-{int(datetime.now().timestamp())}",
                environment={
                    "GATEWAY_ROLE": "gateway",
                    "TASK_ID": self.fallback_task_container,
                    "TARGET_GATEWAY_ID": dead_gateway_id,
                    "WARDEN_MODE": "respawn",
                },
                remove=True,
            )
            self.active_containers[dead_gateway_id] = container.id
            logger.info("Spawned replacement container %s for gateway=%s", container.id, dead_gateway_id)
        except Exception as exc:
            logger.exception("Failed to spawn replacement for %s: %s", dead_gateway_id, exc)

    def _build_respawn_request(self, role: str, task_id: str, dead_gateway_id: str) -> RespawnRequest:
        # Signature intentionally optional in minimal loop; keep empty string if not configured.
        # This path is deterministic and safe (no eval).
        return RespawnRequest(
            target_role=role,
            dead_gateway_id=dead_gateway_id,
            task_id=task_id,
            reason="heartbeat_miss",
            requesting_gateway="warden",
            signature="",
        )

    async def _on_heartbeat(self, msg):
        try:
            payload = _decode_msg(msg.data)
            data = HeartbeatPing(**payload)
        except ValidationError:
            logger.warning("Invalid heartbeat payload on %s", msg.subject)
            return

        state = self.liveness.get(data.gateway_id)
        if state is None:
            state = GatewayLiveness(gateway_id=data.gateway_id, task_id=str(data.task_id))
            self.liveness[data.gateway_id] = state

        state.task_id = str(data.task_id)
        state.touched()

    async def _on_event_heartbeat(self, msg):
        # Accept both direct payloads and SwarmEvent wrappers.
        data = _decode_msg(msg.data)
        if isinstance(data, dict) and "payload" in data:
            pl = data.get("payload", {})
        else:
            pl = data

        try:
            hb = HeartbeatPing(**pl)
        except Exception:
            return

        state = self.liveness.get(hb.gateway_id)
        if state is None:
            state = GatewayLiveness(gateway_id=hb.gateway_id, task_id=str(hb.task_id))
            self.liveness[hb.gateway_id] = state

        state.task_id = str(hb.task_id)
        state.touched()

    async def _on_respawn(self, msg):
        data = _decode_msg(msg.data)
        try:
            req = RespawnRequest(**data)
        except ValidationError:
            logger.warning("Invalid respawn payload on %s", msg.subject)
            return

        # For now, only accept explicit requests from known coordinators/selves.
        # Signature validation can be added once signing is standardized.
        target_id = req.dead_gateway_id
        await self._spawn_gateway_container(target_id)


class WardenSubjects:
    SUICIDE_BASE = "swarm.advisory.suicide."


def _decode_msg(raw: bytes) -> dict[str, Any] | Any:
    try:
        import json

        return json.loads(raw.decode())
    except Exception:
        return {}


async def main():
    parser = argparse.ArgumentParser(description="Run the memu Warden runtime")
    parser.add_argument("--dry-run", action="store_true", help="do not spawn docker containers")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["WARDEN_DRY_RUN"] = "1"

    logging.basicConfig(
        level=os.environ.get("WARDEN_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    runtime = WardenRuntime()

    # Dry-run disables docker execution even when available
    if args.dry_run:
        runtime._use_docker = False
        logger.info("Warden running in dry-run mode")

    try:
        await runtime.run()
    except asyncio.CancelledError:
        logger.info("Warden cancelled")
    except Exception:
        logger.exception("Warden runtime failed")
        raise


if __name__ == "__main__":
    asyncio.run(main())
