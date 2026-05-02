"""Durable AGENT_EVENTS consumer + metrics sink.

Tracks replay/ack/failure and lag-ish metrics for OpenClaw adoption monitoring.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig, ReplayPolicy
from memu.cluster import NATSClusterManager

logger = logging.getLogger(__name__)

# Defaults / env overrides
AGENT_EVENTS_STREAM = os.environ.get("AGENT_EVENTS_STREAM", "AGENT_EVENTS")
AGENT_EVENTS_CONSUMER = os.environ.get(
    "AGENT_EVENTS_CONSUMER", "MEMU_OPENCLAW_ADOPTION_CONSUMER"
)
AGENT_EVENTS_SUBJECT = os.environ.get("AGENT_EVENTS_SUBJECT", "AGENT_EVENTS")
ACK_WAIT_SECONDS = float(os.environ.get("AGENT_EVENTS_ACK_WAIT_SECONDS", "60"))
MAX_ACK_PENDING = int(os.environ.get("AGENT_EVENTS_MAX_ACK_PENDING", "1000"))


@dataclass
class AgentEventsMetrics:
    consumed_count: int = 0
    acked_count: int = 0
    replay_count: int = 0
    failure_count: int = 0
    last_error: str | None = None
    last_processed_ts: float | None = None
    last_error_ts: float | None = None

    @property
    def ack_rate(self) -> float:
        return (self.acked_count / self.consumed_count) if self.consumed_count else 0.0

    @property
    def failure_rate(self) -> float:
        return (self.failure_count / self.consumed_count) if self.consumed_count else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "consumed_count": self.consumed_count,
            "acked_count": self.acked_count,
            "replay_count": self.replay_count,
            "failure_count": self.failure_count,
            "ack_rate": round(self.ack_rate, 6),
            "failure_rate": round(self.failure_rate, 6),
            "last_processed_ts": self.last_processed_ts,
            "last_error": self.last_error,
            "last_error_ts": self.last_error_ts,
        }


class AgentEventsConsumer:
    """Consumes AGENT_EVENTS from JetStream as a durable consumer and tracks metrics."""

    def __init__(
        self,
        stream: str = AGENT_EVENTS_STREAM,
        consumer_name: str = AGENT_EVENTS_CONSUMER,
        subject: str = AGENT_EVENTS_SUBJECT,
        *,
        cluster_manager: NATSClusterManager | None = None,
    ):
        self.stream = stream
        self.consumer_name = consumer_name
        self.subject = subject
        self._manager = cluster_manager or NATSClusterManager()
        self._js: JetStreamContext | None = None
        self._sub = None
        self._task: asyncio.Task | None = None
        self.metrics = AgentEventsMetrics()
        self._running = False

    async def start(self) -> None:
        if self._running:
            return

        await self._manager.connect()
        self._js = self._manager.jetstream
        await self._ensure_stream()
        await self._ensure_consumer()

        self._sub = await self._js.subscribe(
            self.subject,
            durable=self.consumer_name,
            stream=self.stream,
            config=ConsumerConfig(
                durable_name=self.consumer_name,
                ack_wait=ACK_WAIT_SECONDS,
                max_ack_pending=MAX_ACK_PENDING,
                ack_policy=AckPolicy.EXPLICIT,
                replay_policy=ReplayPolicy.INSTANT,
                filter_subject=self.subject,
            ),
            manual_ack=True,
        )

        self._task = asyncio.create_task(self._consume())
        self._running = True
        logger.info("AGENT_EVENTS durable consumer started")

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._sub:
            try:
                await self._sub.unsubscribe()
            except Exception:
                pass

        await self._manager.close()
        logger.info("AGENT_EVENTS durable consumer stopped")

    async def _ensure_stream(self) -> None:
        if not self._js:
            return

        try:
            await self._js.stream_info(self.stream)
            return
        except Exception:
            logger.info("Creating AGENT_EVENTS stream: %s", self.stream)

        await self._js.add_stream(
            name=self.stream,
            subjects=[self.stream],
            max_age=60 * 60 * 24 * 7,
            max_msgs=1_000_000,
            retention="limits",
            storage="file",
        )

    async def _ensure_consumer(self) -> None:
        if not self._js:
            return

        try:
            await self._js.consumer_info(self.stream, self.consumer_name)
        except Exception:
            logger.info(
                "Durable consumer %s will be auto-created on subscribe for %s",
                self.consumer_name,
                self.stream,
            )

    async def _consume(self):
        try:
            async for msg in self._sub.messages:
                await self.process_message(msg)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("AGENT_EVENTS consumer loop error: %s", exc)

    async def process_message(self, msg: Any) -> None:
        self.metrics.consumed_count += 1

        redeliveries = self._redelivery_count(msg)
        if redeliveries:
            self.metrics.replay_count += redeliveries

        try:
            _ = json.loads(msg.data.decode())
            acked = await self._ack(msg)
            if acked:
                self.metrics.acked_count += 1
                self.metrics.last_processed_ts = time.time()
        except Exception as exc:
            self.metrics.failure_count += 1
            self.metrics.last_error = str(exc)
            self.metrics.last_error_ts = time.time()
            await self._nak(msg)

    async def _ack(self, msg: Any) -> bool:
        try:
            result = msg.ack()
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            self.metrics.failure_count += 1
            self.metrics.last_error = str(exc)
            self.metrics.last_error_ts = time.time()
            try:
                result = msg.ack_sync()
                if inspect.isawaitable(result):
                    await result
                return True
            except Exception:
                # caller handles metrics as failure
                return False

    async def _nak(self, msg: Any) -> None:
        try:
            result = msg.nak()
            if inspect.isawaitable(result):
                await result
        except Exception:
            try:
                msg.term()
            except Exception:
                pass

    def _redelivery_count(self, msg: Any) -> int:
        meta = getattr(msg, "metadata", None)
        if not meta:
            return 0

        value = getattr(meta, "num_delivered", 0)
        try:
            return max(0, int(value) - 1)
        except (TypeError, ValueError):
            return 0

    async def health_artifact(self) -> dict[str, Any]:
        artifact: dict[str, Any] = {
            "source": "AGENT_EVENTS",
            "stream": self.stream,
            "consumer": self.consumer_name,
            "durable_active": self._running,
            "lag": await self._current_lag(),
            "metrics": self.metrics.as_dict(),
        }

        try:
            artifact["cluster"] = self._manager.status()
            artifact["active_node"] = self._manager.active_node.value
            artifact["active_node_connected"] = self._manager.active_connection.is_connected
        except Exception:
            artifact["cluster"] = None
            artifact["active_node"] = None
            artifact["active_node_connected"] = False

        return artifact

    async def _current_lag(self) -> int | None:
        if not self._js:
            return None

        try:
            cinfo = await self._js.consumer_info(self.stream, self.consumer_name)
            if isinstance(cinfo, dict):
                for key in ("num_ack_pending", "num_waiting", "num_pending", "num_delivery"):
                    value = cinfo.get(key)
                    if isinstance(value, (int, float)):
                        return max(0, int(value))
            else:
                for key in ("num_ack_pending", "num_waiting", "num_pending", "num_delivery"):
                    value = getattr(cinfo, key, None)
                    if isinstance(value, (int, float)):
                        return max(0, int(value))
        except Exception:
            pass

        return None
