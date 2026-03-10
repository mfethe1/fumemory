"""Gateway status sync manager backed by JetStream KV + swarm.gateway.* events.

This replaces the old fire-and-forget pulse helper with a retained status plane:
- JetStream KV keeps the latest state for every gateway
- swarm.gateway.* subjects provide real-time event fan-out
- warm-standby gateways stay visible without cron-based sweeps
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import nats
from nats.js import JetStreamContext
from nats.js.api import KeyValueConfig
from nats.js.kv import KeyValue

from memu.cluster import NATSClusterManager
from memu.swarm_models import GatewayAnnounce, GatewayStatus

logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
GATEWAY_STATE_BUCKET = os.environ.get("GATEWAY_STATE_BUCKET", "GATEWAY_STATE")
GLOBAL_SETTINGS_BUCKET = os.environ.get("GLOBAL_SETTINGS_BUCKET", "GLOBAL_SETTINGS")
DEFAULT_PULSE_INTERVAL_S = int(os.environ.get("GATEWAY_STATUS_PULSE_INTERVAL_S", "10"))
DEFAULT_STALE_AFTER_S = int(os.environ.get("GATEWAY_STATUS_STALE_AFTER_S", "20"))
DEFAULT_OFFLINE_AFTER_S = int(os.environ.get("GATEWAY_STATUS_OFFLINE_AFTER_S", "60"))

GATEWAY_REGISTER_SUBJECT = "swarm.gateway.register"
GATEWAY_HEARTBEAT_SUBJECT = "swarm.gateway.heartbeat"
GATEWAY_STATUS_SUBJECT = "swarm.gateway.status"
LEGACY_DISCOVERY_SUBJECT = "swarm.discovery"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _as_gateway_status(raw: str | None) -> str:
    if not raw:
        return GatewayStatus.OFFLINE.value
    return raw


def _effective_gateway_status(
    reported_status: str,
    age_seconds: float | None,
    *,
    stale_after_s: int,
    offline_after_s: int,
) -> str:
    if reported_status in {GatewayStatus.OFFLINE.value, GatewayStatus.DRAINING.value}:
        return reported_status
    if age_seconds is None:
        return GatewayStatus.OFFLINE.value
    if age_seconds >= offline_after_s:
        return GatewayStatus.OFFLINE.value
    if age_seconds >= stale_after_s:
        return GatewayStatus.DEGRADED.value
    return reported_status or GatewayStatus.ONLINE.value


async def ensure_kv_bucket(
    js: JetStreamContext,
    bucket: str,
    *,
    history: int = 5,
) -> KeyValue:
    """Bind to a KV bucket, creating it when missing."""
    try:
        return await js.key_value(bucket)
    except Exception:
        return await js.create_key_value(
            KeyValueConfig(bucket=bucket, history=history)
        )


async def build_gateway_status_overview(
    js: JetStreamContext,
    *,
    cluster_status: dict[str, Any] | None = None,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
    offline_after_s: int = DEFAULT_OFFLINE_AFTER_S,
) -> dict[str, Any]:
    """Build an operational view of the gateway mesh from retained JetStream KV state."""
    state_kv = await ensure_kv_bucket(js, GATEWAY_STATE_BUCKET)
    now = _utc_now()

    try:
        keys = sorted(await state_kv.keys() or [])
    except Exception:
        keys = []

    gateways: list[dict[str, Any]] = []
    for gateway_id in keys:
        try:
            entry = await state_kv.get(gateway_id)
            state = json.loads(entry.value.decode())
        except Exception as exc:
            logger.warning("Unable to read gateway state for %s: %s", gateway_id, exc)
            continue

        last_seen = _parse_timestamp(
            state.get("last_heartbeat_at")
            or state.get("last_pulse")
            or state.get("updated_at")
        )
        age_seconds = None if last_seen is None else max((now - last_seen).total_seconds(), 0.0)
        reported_status = _as_gateway_status(state.get("status"))
        effective_status = _effective_gateway_status(
            reported_status,
            age_seconds,
            stale_after_s=stale_after_s,
            offline_after_s=offline_after_s,
        )

        gateway = {
            **state,
            "gateway_id": state.get("gateway_id", gateway_id),
            "reported_status": reported_status,
            "effective_status": effective_status,
            "last_heartbeat_at": last_seen.isoformat() if last_seen else None,
            "age_seconds": None if age_seconds is None else round(age_seconds, 2),
            "stale": effective_status == GatewayStatus.DEGRADED.value and reported_status != GatewayStatus.DEGRADED.value,
        }
        gateways.append(gateway)

    gateways.sort(key=lambda item: (item["effective_status"], item["gateway_id"]))

    summary = {
        "total": len(gateways),
        "online": sum(1 for g in gateways if g["effective_status"] == GatewayStatus.ONLINE.value),
        "degraded": sum(1 for g in gateways if g["effective_status"] == GatewayStatus.DEGRADED.value),
        "offline": sum(1 for g in gateways if g["effective_status"] == GatewayStatus.OFFLINE.value),
        "draining": sum(1 for g in gateways if g["effective_status"] == GatewayStatus.DRAINING.value),
        "stale_gateways": [g["gateway_id"] for g in gateways if g.get("stale")],
        "active_tasks": sum(1 for g in gateways if g.get("current_task_id")),
    }

    alerts: list[dict[str, Any]] = []
    if cluster_status:
        active_node = cluster_status.get("active_node")
        if active_node and active_node != "local":
            alerts.append({
                "severity": "warning",
                "code": "cluster_on_fallback",
                "message": f"Active NATS node is {active_node}",
            })

        disconnected_nodes = [
            node for node, info in (cluster_status.get("nodes") or {}).items() if not info.get("connected")
        ]
        if disconnected_nodes:
            alerts.append({
                "severity": "warning",
                "code": "cluster_nodes_disconnected",
                "message": "One or more NATS nodes are disconnected",
                "nodes": disconnected_nodes,
            })

    if summary["stale_gateways"]:
        alerts.append({
            "severity": "warning",
            "code": "gateway_stale",
            "message": "One or more gateways have stale heartbeats",
            "gateways": summary["stale_gateways"],
        })

    unexpected_offline = [
        g["gateway_id"]
        for g in gateways
        if g["effective_status"] == GatewayStatus.OFFLINE.value
        and g.get("reported_status") not in {GatewayStatus.OFFLINE.value, GatewayStatus.DRAINING.value}
    ]
    if unexpected_offline:
        alerts.append({
            "severity": "critical",
            "code": "gateway_offline",
            "message": "One or more gateways disappeared from the mesh",
            "gateways": unexpected_offline,
        })

    return {
        "generated_at": now.isoformat(),
        "cluster": cluster_status or {},
        "summary": summary,
        "gateways": gateways,
        "alerts": alerts,
    }


class StateManager:
    def __init__(
        self,
        gateway_id: str,
        nats_url: str = NATS_URL,
        *,
        cluster: NATSClusterManager | None = None,
        role: str | None = None,
    ):
        self.gateway_id = gateway_id
        self.nats_url = nats_url
        self.cluster = cluster
        self.role = role
        self.nc = None
        self.js = None
        self.state_kv: KeyValue | None = None
        self.settings_kv: KeyValue | None = None
        self._pulse_task: asyncio.Task | None = None
        self._last_state: dict[str, Any] = {}
        self._last_revision: int = 0
        self._cached_metadata: dict[str, Any] = {}

    async def _ensure_bindings(self):
        if self.cluster:
            try:
                nc = self.cluster.active_connection
                js = self.cluster.jetstream
            except Exception:
                await self.cluster.connect()
                nc = self.cluster.active_connection
                js = self.cluster.jetstream
            if self.nc is not nc or self.js is not js or not self.state_kv or not self.settings_kv:
                self.nc = nc
                self.js = js
                self.state_kv = await ensure_kv_bucket(js, GATEWAY_STATE_BUCKET)
                self.settings_kv = await ensure_kv_bucket(js, GLOBAL_SETTINGS_BUCKET)
        elif not self.nc or not self.js or not self.state_kv or not self.settings_kv:
            self.nc = await nats.connect(self.nats_url)
            self.js = self.nc.jetstream()
            self.state_kv = await ensure_kv_bucket(self.js, GATEWAY_STATE_BUCKET)
            self.settings_kv = await ensure_kv_bucket(self.js, GLOBAL_SETTINGS_BUCKET)

    async def connect(self):
        await self._ensure_bindings()
        logger.info("StateManager connected for %s", self.gateway_id)

    def _cluster_snapshot(self) -> dict[str, Any]:
        if self.cluster:
            return self.cluster.status()
        return {
            "active_node": "standalone",
            "nodes": {
                "standalone": {
                    "url": self.nats_url,
                    "connected": bool(self.nc and self.nc.is_connected),
                    "latency_ms": 0.0,
                    "last_error": None,
                    "reconnects": 0,
                    "last_seen": _utc_now().timestamp(),
                }
            },
        }

    def _build_state(
        self,
        status: GatewayStatus,
        *,
        current_task_id: UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cluster_snapshot = self._cluster_snapshot()
        merged_metadata = {**self._cached_metadata, **(metadata or {})}
        sync_revision = self._last_revision + 1
        current_task = current_task_id if current_task_id is not None else self._last_state.get("current_task_id")

        return {
            "gateway_id": self.gateway_id,
            "role": self.role,
            "status": status.value,
            "current_task_id": str(current_task) if current_task else None,
            "metadata": merged_metadata,
            "last_heartbeat_at": _iso_now(),
            "last_pulse": _iso_now(),
            "active_nats_node": cluster_snapshot.get("active_node"),
            "nats_nodes": cluster_snapshot.get("nodes", {}),
            "sync_revision": sync_revision,
        }

    async def _publish_json(self, subject: str, payload: dict[str, Any]):
        await self._ensure_bindings()
        if self.nc and self.nc.is_connected:
            await self.nc.publish(subject, json.dumps(payload).encode())

    async def _publish_status_snapshot(self, state: dict[str, Any], *, reason: str):
        payload = {**state, "reason": reason}
        await self._publish_json(GATEWAY_HEARTBEAT_SUBJECT, payload)
        if reason in {"draining", "offline", "failover"}:
            await self._publish_json(GATEWAY_STATUS_SUBJECT, payload)

    async def register_gateway(
        self,
        *,
        capabilities: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        model_id: str | None = None,
        max_concurrent_tasks: int = 5,
        status: GatewayStatus = GatewayStatus.ONLINE,
        current_task_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_bindings()

        announce = GatewayAnnounce(
            gateway_id=self.gateway_id,
            capabilities=capabilities or [],
            status=status,
            max_concurrent_tasks=max_concurrent_tasks,
            model_id=model_id,
            metadata={**(metadata or {}), "role": self.role},
        )

        capability_names = []
        for capability in announce.capabilities:
            if hasattr(capability, "name"):
                capability_names.append(capability.name)
            elif isinstance(capability, dict):
                capability_names.append(capability.get("name", "unknown"))
            else:
                capability_names.append(str(capability))

        state = self._build_state(
            status,
            current_task_id=current_task_id,
            metadata={
                **announce.metadata,
                "capabilities": capability_names,
                "model_id": model_id,
                "max_concurrent_tasks": max_concurrent_tasks,
                "registered_at": _iso_now(),
            },
        )
        self._cached_metadata = state["metadata"]
        self._last_revision = await self.state_kv.put(self.gateway_id, json.dumps(state).encode())
        self._last_state = state

        payload = announce.model_dump(mode="json")
        await self._publish_json(GATEWAY_REGISTER_SUBJECT, payload)
        await self._publish_json(LEGACY_DISCOVERY_SUBJECT, payload)
        await self._publish_status_snapshot(state, reason="register")
        return state

    async def update_state(
        self,
        status: GatewayStatus,
        current_task_id: UUID | str | None = None,
        metadata: dict | None = None,
        *,
        reason: str = "pulse",
    ) -> dict[str, Any]:
        await self._ensure_bindings()
        if not self.state_kv:
            return {}

        state = self._build_state(status, current_task_id=current_task_id, metadata=metadata)
        self._cached_metadata = state["metadata"]
        self._last_revision = await self.state_kv.put(self.gateway_id, json.dumps(state).encode())
        self._last_state = state
        await self._publish_status_snapshot(state, reason=reason)
        return state

    async def mark_draining(
        self,
        *,
        current_task_id: UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.update_state(
            GatewayStatus.DRAINING,
            current_task_id=current_task_id,
            metadata=metadata,
            reason="draining",
        )

    async def mark_offline(
        self,
        *,
        current_task_id: UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.update_state(
            GatewayStatus.OFFLINE,
            current_task_id=current_task_id,
            metadata=metadata,
            reason="offline",
        )

    async def start_pulse(self, interval_s: int = DEFAULT_PULSE_INTERVAL_S):
        """Start a background task that refreshes retained mesh state."""
        if self._pulse_task:
            return

        async def _pulse_loop():
            while True:
                try:
                    await self.update_state(
                        GatewayStatus.ONLINE,
                        current_task_id=self._last_state.get("current_task_id"),
                        metadata=self._cached_metadata,
                        reason="pulse",
                    )
                except Exception as exc:
                    logger.error("Failed to pulse gateway state for %s: %s", self.gateway_id, exc)
                await asyncio.sleep(interval_s)

        self._pulse_task = asyncio.create_task(_pulse_loop())
        logger.info("Started state pulse for %s", self.gateway_id)

    async def stop_pulse(self):
        if self._pulse_task:
            self._pulse_task.cancel()
            await asyncio.gather(self._pulse_task, return_exceptions=True)
            self._pulse_task = None

    async def get_gateway_state(self, gateway_id: str) -> dict | None:
        await self._ensure_bindings()
        try:
            entry = await self.state_kv.get(gateway_id)
            return json.loads(entry.value.decode())
        except Exception:
            return None

    async def get_status_overview(
        self,
        *,
        stale_after_s: int = DEFAULT_STALE_AFTER_S,
        offline_after_s: int = DEFAULT_OFFLINE_AFTER_S,
    ) -> dict[str, Any]:
        await self._ensure_bindings()
        return await build_gateway_status_overview(
            self.js,
            cluster_status=self._cluster_snapshot(),
            stale_after_s=stale_after_s,
            offline_after_s=offline_after_s,
        )

    async def list_gateway_states(
        self,
        *,
        stale_after_s: int = DEFAULT_STALE_AFTER_S,
        offline_after_s: int = DEFAULT_OFFLINE_AFTER_S,
    ) -> list[dict[str, Any]]:
        overview = await self.get_status_overview(
            stale_after_s=stale_after_s,
            offline_after_s=offline_after_s,
        )
        return overview["gateways"]

    async def close(self):
        await self.stop_pulse()
        if self.cluster:
            return
        if self.nc:
            await self.nc.close()
