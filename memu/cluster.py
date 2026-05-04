"""NATS Cluster Connection Manager — Dual-instance failover.

Primary: Local NATS instance (low latency, fast path)
Fallback: Railway NATS instance (redundant, survives local outages)

Gateways connect to both. If local dies, traffic seamlessly
routes to Railway. When local recovers, it becomes primary again.

Usage:
    manager = NATSClusterManager(
        local_url="nats://localhost:4222",
        railway_url="nats://nats.railway.internal:4222",
    )
    async with manager:
        js = manager.jetstream
        nc = manager.active_connection
        # ... use normally
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse
from typing import Any, Callable, Coroutine

import nats
from nats.aio.client import Client as NATSClient
from nats.js import JetStreamContext

logger = logging.getLogger(__name__)


class ClusterNode(str, Enum):
    LOCAL = "local"
    RAILWAY = "railway"


@dataclass
class NodeHealth:
    node: ClusterNode
    url: str
    connected: bool = False
    last_ping_ms: float = 0.0
    last_error: str | None = None
    reconnect_count: int = 0
    last_seen: float = field(default_factory=time.time)


class NATSClusterManager:
    """Manages dual NATS connections with automatic failover."""

    @staticmethod
    def _clean_url(value: str | None) -> str | None:
        if not value:
            return None
        value = value.strip()
        if not value or value.lower() in {"none", "null", "undefined", "-", "-1"}:
            return None
        # Ignore known placeholder hostnames that aren't valid in production envs
        placeholder_hosts = {
            "nats",
            "nats.railway.internal",
            "nats-railway.railway.internal",
        }
        parsed = urlparse(value)
        host = parsed.hostname
        if not host:
            return value
        if host in placeholder_hosts:
            return None
        return value

    def __init__(
        self,
        local_url: str | None = None,
        railway_url: str | None = None,
        ping_interval_s: float = 5.0,
        failover_threshold_ms: float = 500.0,
        auth_token: str | None = None,
        connect_timeout_s: float = 5.0,
        max_reconnect_attempts: int = -1,
        allow_reconnect: bool = True,
    ):
        self.local_url = self._clean_url(local_url or os.environ.get("NATS_LOCAL_URL"))
        self.railway_url = self._clean_url(railway_url or os.environ.get("NATS_RAILWAY_URL"))
        self.ping_interval_s = ping_interval_s
        self.failover_threshold_ms = failover_threshold_ms
        self.auth_token = auth_token or os.environ.get("NATS_AUTH_TOKEN")
        self.connect_timeout_s = connect_timeout_s
        self.max_reconnect_attempts = max_reconnect_attempts
        self.allow_reconnect = allow_reconnect

        self._connections: dict[ClusterNode, NATSClient] = {}
        self._jetstreams: dict[ClusterNode, JetStreamContext] = {}
        self._active_node: ClusterNode = ClusterNode.LOCAL
        self._monitor_task: asyncio.Task | None = None
        self._on_failover_callbacks: list[Callable] = []

        self._health: dict[ClusterNode, NodeHealth] = {
            ClusterNode.LOCAL: NodeHealth(node=ClusterNode.LOCAL, url=self.local_url or ""),
            ClusterNode.RAILWAY: NodeHealth(node=ClusterNode.RAILWAY, url=self.railway_url or ""),
        }

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    @property
    def active_connection(self) -> NATSClient:
        """Get the currently active NATS connection."""
        nc = self._connections.get(self._active_node)
        if nc and nc.is_connected:
            return nc
        # Failover to the other node
        other = (
            ClusterNode.RAILWAY
            if self._active_node == ClusterNode.LOCAL
            else ClusterNode.LOCAL
        )
        nc = self._connections.get(other)
        if nc and nc.is_connected:
            self._switch_to(other)
            return nc
        raise ConnectionError("No NATS connections available")

    @property
    def jetstream(self) -> JetStreamContext:
        """Get JetStream context for the active connection."""
        js = self._jetstreams.get(self._active_node)
        if js:
            return js
        other = (
            ClusterNode.RAILWAY
            if self._active_node == ClusterNode.LOCAL
            else ClusterNode.LOCAL
        )
        js = self._jetstreams.get(other)
        if js:
            self._switch_to(other)
            return js
        raise ConnectionError("No JetStream contexts available")

    @property
    def active_node(self) -> ClusterNode:
        return self._active_node

    @property
    def health(self) -> dict[ClusterNode, NodeHealth]:
        return self._health.copy()

    def on_failover(self, callback: Callable):
        """Register a callback for failover events."""
        self._on_failover_callbacks.append(callback)

    async def connect(self):
        """Connect to both NATS instances concurrently."""
        connect_targets = []
        if self.local_url:
            connect_targets.append((ClusterNode.LOCAL, self.local_url))
        if self.railway_url:
            connect_targets.append((ClusterNode.RAILWAY, self.railway_url))

        if not connect_targets:
            raise ConnectionError("No NATS URLs configured")

        tasks = [
            self._connect_node(node, url)
            for node, url in connect_targets
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        connected = sum(1 for r in results if not isinstance(r, Exception))

        if connected == 0:
            raise ConnectionError(
                "Failed to connect to any NATS instance. "
                f"Configured local={self.local_url}, railway={self.railway_url}"
            )

        # Prefer local if both connected
        if self._health[ClusterNode.LOCAL].connected:
            self._active_node = ClusterNode.LOCAL
        else:
            self._active_node = ClusterNode.RAILWAY

        logger.info(
            f"NATS cluster connected. Active: {self._active_node.value}. "
            f"Local: {'✓' if self._health[ClusterNode.LOCAL].connected else '✗'}, "
            f"Railway: {'✓' if self._health[ClusterNode.RAILWAY].connected else '✗'}"
        )

        # Start health monitor
        self._monitor_task = asyncio.create_task(self._health_monitor())

    async def _connect_node(self, node: ClusterNode, url: str):
        """Connect to a single NATS node with error handling.

        Enforces IPv4 preference to avoid IPv6/IPv4 connection issues.
        """
        health = self._health[node]

        try:
            # Parse URL to extract host and port for IPv4 resolution
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 4222

            # Resolve to IPv4 explicitly to avoid IPv6 connection issues
            import socket
            try:
                # Try to resolve to IPv4 first
                addr_info = socket.getaddrinfo(
                    host, port,
                    socket.AF_INET,  # Force IPv4
                    socket.SOCK_STREAM
                )
                if addr_info:
                    ipv4_host = addr_info[0][4][0]
                    # Reconstruct URL with IPv4 address
                    url = f"nats://{ipv4_host}:{port}"
                    logger.debug(f"Resolved {host} to IPv4: {ipv4_host}")
            except socket.gaierror as e:
                logger.warning(f"IPv4 resolution failed for {host}, using original URL: {e}")

            connect_opts: dict[str, Any] = {
                "servers": [url],
                "connect_timeout": self.connect_timeout_s,
                "reconnect_time_wait": 2,
                "max_reconnect_attempts": self.max_reconnect_attempts,
                "ping_interval": 10,
                "max_outstanding_pings": 3,
                "allow_reconnect": self.allow_reconnect,
            }

            if self.auth_token:
                connect_opts["token"] = self.auth_token

            # Synadia Cloud / NGS credential-based auth
            creds_file = os.environ.get("NATS_CREDS_FILE")
            nkey_seed = os.environ.get("NATS_NKEY_SEED")
            if creds_file:
                connect_opts["user_credentials"] = creds_file
            elif nkey_seed:
                connect_opts["nkeys_seed"] = nkey_seed

            # Error callbacks
            async def on_error(e):
                logger.error(f"NATS {node.value} error: {e}")
                health.last_error = str(e)

            async def on_disconnect():
                logger.warning(f"NATS {node.value} disconnected")
                health.connected = False
                # Trigger failover if this was the active node
                if self._active_node == node:
                    await self._failover()

            async def on_reconnect():
                logger.info(f"NATS {node.value} reconnected")
                health.connected = True
                health.reconnect_count += 1
                health.last_seen = time.time()

            connect_opts["error_cb"] = on_error
            connect_opts["disconnected_cb"] = on_disconnect
            connect_opts["reconnected_cb"] = on_reconnect

            nc = await nats.connect(**connect_opts)
            self._connections[node] = nc
            self._jetstreams[node] = nc.jetstream()

            health.connected = True
            health.last_seen = time.time()
            health.last_error = None

            logger.info(f"Connected to NATS {node.value} at {url}")

        except Exception as e:
            health.connected = False
            health.last_error = str(e)
            logger.warning(f"Failed to connect to NATS {node.value} at {url}: {e}")
            raise

    async def _failover(self):
        """Switch to the other NATS node."""
        other = (
            ClusterNode.RAILWAY
            if self._active_node == ClusterNode.LOCAL
            else ClusterNode.LOCAL
        )

        if self._health[other].connected:
            old = self._active_node
            self._switch_to(other)
            logger.warning(f"NATS FAILOVER: {old.value} → {other.value}")

            # Notify callbacks
            for cb in self._on_failover_callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(old, other)
                    else:
                        cb(old, other)
                except Exception as e:
                    logger.error(f"Failover callback error: {e}")
        else:
            logger.error("NATS FAILOVER FAILED: no healthy nodes available")

    def _switch_to(self, node: ClusterNode):
        """Switch the active node."""
        self._active_node = node
        logger.info(f"Active NATS node: {node.value}")

    async def _health_monitor(self):
        """Background health check — ping both nodes, detect latency drift."""
        while True:
            try:
                await asyncio.sleep(self.ping_interval_s)

                for node, nc in self._connections.items():
                    health = self._health[node]
                    try:
                        if not nc.is_connected:
                            health.connected = False
                            continue

                        start = time.monotonic()
                        await nc.flush(timeout=2)
                        latency_ms = (time.monotonic() - start) * 1000

                        health.connected = True
                        health.last_ping_ms = latency_ms
                        health.last_seen = time.time()
                        health.last_error = None

                    except Exception as e:
                        health.connected = False
                        health.last_error = str(e)

                # Auto-recovery: if local is back and healthier, switch back
                local_h = self._health[ClusterNode.LOCAL]
                railway_h = self._health[ClusterNode.RAILWAY]

                if (
                    self._active_node == ClusterNode.RAILWAY
                    and local_h.connected
                    and local_h.last_ping_ms < self.failover_threshold_ms
                ):
                    logger.info("Local NATS recovered — switching back to local")
                    self._switch_to(ClusterNode.LOCAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health monitor error: {e}")

    async def close(self):
        """Gracefully close all connections."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        for node, nc in self._connections.items():
            try:
                await nc.drain()
                await nc.close()
                logger.info(f"Closed NATS {node.value}")
            except Exception as e:
                logger.warning(f"Error closing NATS {node.value}: {e}")

    def status(self) -> dict[str, Any]:
        """Return cluster status for the Glass Box UI."""
        return {
            "active_node": self._active_node.value,
            "nodes": {
                node.value: {
                    "url": h.url,
                    "connected": h.connected,
                    "latency_ms": round(h.last_ping_ms, 2),
                    "last_error": h.last_error,
                    "reconnects": h.reconnect_count,
                    "last_seen": h.last_seen,
                }
                for node, h in self._health.items()
            },
        }
