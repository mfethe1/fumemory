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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from memu.cluster import NATSClusterManager
from memu.swarm_models import (
    HeartbeatPing,
    SwarmEvent,
    EventType,
    TaskOrphaned,
)
from memu.warden import RespawnRequest
from memu.sandbox import get_sandbox_provider, SandboxProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OPA Policy Hook (Track #1 — Lenny wires this)
# ---------------------------------------------------------------------------
async def _policy_check(action: str, context: dict) -> tuple[bool, str]:
    """Gate all Warden actions through OPA/Cedar policy engine."""
    from memu.policy.opa_client import OPAClient
    return await OPAClient.evaluate("memu/warden/allow", {"action": action, **context})


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
        self._respawn_retry_after: dict[str, float] = {}
        self._spawn_inflight: set[str] = set()
        self._respawn_cooldown_s = int(os.environ.get("WARDEN_RESPAWN_COOLDOWN_S", "30"))

        # Pluggable sandbox provider (docker | e2b | extism | none)
        self._sandbox: SandboxProvider = get_sandbox_provider()
        logger.info("Warden sandbox provider: %s", self._sandbox.name)

        # Optional docker integration (non-fatal if missing) — kept for gateway respawn
        # Sandbox execution now goes through self._sandbox (see memu/sandbox/)
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
            self._heartbeat_receiver(),
            self._respawn_receiver(),
        )

    async def _subscribe(self):
        assert self.cluster is not None
        nc = self.cluster.active_connection
        logger.info("Warden NATS connection: connected=%s, client_id=%s", nc.is_connected, nc.client_id)

        # Use poll-based subscriptions (nats-py callback subs don't fire reliably
        # when mixed with asyncio.gather event loops).
        self._hb_sub = await nc.subscribe("swarm.warden.heartbeat")
        self._respawn_sub = await nc.subscribe("swarm.warden.respawn")
        self._event_hb_sub = await nc.subscribe("swarm.events.heartbeat")

        logger.info("Warden subscribed (poll mode) to heartbeat + respawn + events.heartbeat")

    async def _heartbeat_receiver(self):
        """Poll-based heartbeat receiver loop."""
        while True:
            try:
                msg = await self._hb_sub.next_msg(timeout=5)
                logger.debug("GOT HB: %s", msg.data.decode())
                await self._on_heartbeat(msg)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.warning("Heartbeat receiver error: %s", e)
            # Also drain event heartbeat sub
            try:
                msg = await self._event_hb_sub.next_msg(timeout=0.1)
                await self._on_event_heartbeat(msg)
            except Exception:
                pass

    async def _respawn_receiver(self):
        """Poll-based respawn receiver loop."""
        while True:
            try:
                msg = await self._respawn_sub.next_msg(timeout=5)
                await self._on_respawn(msg)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                logger.warning("Respawn receiver error: %s", e)

    async def _heartbeat_watchdog(self):
        while True:
            now = datetime.now(timezone.utc)
            for gateway_id, state in list(self.liveness.items()):
                age = (now - state.last_seen).total_seconds()
                if age <= self.heartbeat_ttl:
                    continue

                logger.warning("Heartbeat expired for %s (age=%.1fs)", gateway_id, age)
                task_id = state.task_id
                await self._handle_expired_gateway(state, task_id)
                # remove to prevent repeated respawn loops
                del self.liveness[gateway_id]

            await asyncio.sleep(self.check_interval)

    async def _handle_expired_gateway(self, state: GatewayLiveness, task_id: str | None = None):
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

        # Basic respawn semantics via single-path trigger
        if state.task_id:
            await self._request_respawn(state.gateway_id, task_id=state.task_id, reason="heartbeat_miss")

        # Try to spawn replacement container (deterministic, single attempt per expiry window)
        await self._spawn_gateway_container(state.gateway_id, task_id or None, requested_from="heartbeat")

    async def _request_respawn(self, dead_gateway_id: str, task_id: str | None = None, reason: str = "manual"):
        if not task_id:
            logger.debug("Skipping respawn event for %s: no task_id", dead_gateway_id)
            return
        if not self._can_spawn(dead_gateway_id):
            return

        assert self.cluster is not None
        nc = self.cluster.active_connection
        req = self._build_respawn_request("gateway", task_id, dead_gateway_id, reason=reason)
        await nc.publish("swarm.warden.respawn", req.model_dump_json().encode())

    async def _spawn_gateway_container(self, dead_gateway_id: str, task_id: str | None = None, requested_from: str = "direct"):
        # --- OPA/Cedar policy gate (Lenny wires full implementation) ---
        allowed, reason = await _policy_check("spawn_gateway", {
            "gateway_id": dead_gateway_id,
            "task_id": task_id,
            "requested_from": requested_from,
            "active_containers": len(self.active_containers),
            "max_containers": self.max_containers,
        })
        if not allowed:
            logger.warning("POLICY DENIED spawn for %s: %s", dead_gateway_id, reason)
            return

        if not self._use_docker:
            logger.warning("Docker unavailable; cannot spawn gateway container for %s", dead_gateway_id)
            return

        if len(self.active_containers) >= self.max_containers:
            logger.error("FORK-BOMB BLOCKER: max containers=%s reached", self.max_containers)
            return

        if self._respawn_lock(dead_gateway_id, requested_from=requested_from):
            return

        try:
            cli = self._docker.from_env()
            container = cli.containers.run(
                image=self.gateway_image,
                detach=True,
                name=f"ward-respawn-{dead_gateway_id}-{str(uuid4())[:8]}",
                network_mode="host",
                environment={
                    "GATEWAY_ROLE": "gateway",
                    "TASK_ID": task_id or self.fallback_task_container,
                    "TARGET_GATEWAY_ID": dead_gateway_id,
                    "WARDEN_MODE": "respawn",
                    "GATEWAY_ID": f"{dead_gateway_id}-respawn",
                    "NATS_LOCAL_URL": os.environ.get("NATS_LOCAL_URL", "nats://127.0.0.1:4222"),
                    "NATS_RAILWAY_URL": os.environ.get("NATS_RAILWAY_URL", "nats://127.0.0.1:4222"),
                },
                remove=True,
            )
            self.active_containers[dead_gateway_id] = container.id
            self._spawn_inflight.discard(dead_gateway_id)
            self._respawn_retry_after.pop(dead_gateway_id, None)
            logger.info("Spawned replacement container %s for gateway=%s (%s) request=%s", container.id, dead_gateway_id, requested_from, task_id or "<none>")
        except Exception as exc:
            self._respawn_retry_after[dead_gateway_id] = time.time() + self._respawn_cooldown_s
            self._spawn_inflight.discard(dead_gateway_id)
            logger.exception("Failed to spawn replacement for %s: %s", dead_gateway_id, exc)

    def _respawn_lock(self, dead_gateway_id: str, requested_from: str = "direct") -> bool:
        """Return True when a new spawn must be skipped."""
        if len(self._spawn_inflight) >= self.max_containers:
            logger.error("Spawn gate: max in-flight/full containers reached=%s", self.max_containers)
            return True

        now = time.time()
        unblock_at = self._respawn_retry_after.get(dead_gateway_id, 0.0)
        if now < unblock_at:
            logger.info("Respawn skipped for %s (%s): cooldown %.1fs remaining", dead_gateway_id, requested_from, unblock_at - now)
            return True

        if dead_gateway_id in self._spawn_inflight:
            logger.debug("Respawn already in-flight for %s (%s)", dead_gateway_id, requested_from)
            return True

        # If a prior respawn container is still present, avoid duplicate spawn attempts.
        if not self._use_docker:
            return False

        try:
            cli = self._docker.from_env()
            existing = [
                c
                for c in cli.containers.list(all=True)
                if c.name.startswith(f"ward-respawn-{dead_gateway_id}-")
            ]
            if existing:
                logger.info("Respawn skipped for %s (%s): existing container(s)=%s", dead_gateway_id, requested_from, [c.name for c in existing])
                return True
        except Exception as exc:
            logger.warning("Unable to scan existing respawn containers for %s: %s", dead_gateway_id, exc)

        self._spawn_inflight.add(dead_gateway_id)
        return False

    def _can_spawn(self, dead_gateway_id: str) -> bool:
        now = time.time()
        if dead_gateway_id in self._spawn_inflight:
            return False
        if now < self._respawn_retry_after.get(dead_gateway_id, 0.0):
            return False
        return True

    def _build_respawn_request(self, role: str, task_id: str, dead_gateway_id: str, reason: str = "heartbeat_miss") -> RespawnRequest:
        # Signature intentionally optional in minimal loop; keep empty string if not configured.
        # This path is deterministic and safe (no eval).
        return RespawnRequest(
            target_role=role,
            dead_gateway_id=dead_gateway_id,
            task_id=task_id,
            reason=reason,
            requesting_gateway="warden",
            signature="",
        )

    async def _on_heartbeat(self, msg):
        try:
            payload = _decode_msg(msg.data)
            data = HeartbeatPing(**payload)
        except (ValidationError, Exception) as e:
            logger.warning("Invalid heartbeat payload on %s: %s — raw: %s", msg.subject, e, msg.data[:200])
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

        if not isinstance(pl, dict):
            return

        # Allow heartbeat wrappers that omit strict fields in lane-lock events.
        task_id = pl.get("task_id")

        if not pl.get("gateway_id") or task_id is None:
            return

        state = self.liveness.get(pl["gateway_id"])
        if state is None:
            state = GatewayLiveness(gateway_id=pl["gateway_id"], task_id=str(task_id))
            self.liveness[pl["gateway_id"]] = state

        state.task_id = str(task_id)
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
        req_task_id = str(req.task_id) if req.task_id else None
        await self._spawn_gateway_container(target_id, req_task_id)


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
