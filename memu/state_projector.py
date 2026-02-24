"""Durable STATE_EVENTS consumer + memU state projection sink.

Consumes state changes from JetStream and applies idempotent upserts into
memU (`state_events`). Tracks lag/ack/replay/failure metrics and exposes a
health artifact for monitoring.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

import asyncpg

from nats.js import JetStreamContext
from memu.cluster import NATSClusterManager

logger = logging.getLogger(__name__)

# Defaults / env overrides
STATE_EVENTS_STREAM = os.environ.get("STATE_EVENTS_STREAM", "STATE_EVENTS")
STATE_EVENTS_CONSUMER = os.environ.get(
    "STATE_EVENTS_CONSUMER", "STATE_PROJECTOR"
)
STATE_EVENTS_SUBJECT = os.environ.get("STATE_EVENTS_SUBJECT", "state.>")
STATE_EVENTS_ACK_WAIT_SECONDS = float(os.environ.get("STATE_EVENTS_ACK_WAIT_SECONDS", "60"))
STATE_EVENTS_MAX_ACK_PENDING = int(os.environ.get("STATE_EVENTS_MAX_ACK_PENDING", "1000"))
STATE_EVENTS_TABLE = os.environ.get("STATE_EVENTS_TABLE", "state_events")


@dataclass
class StateProjectorMetrics:
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


class StateProjectorConsumer:
    """Consumes STATE_EVENTS from JetStream and writes idempotent records."""

    def __init__(
        self,
        stream: str = STATE_EVENTS_STREAM,
        consumer_name: str = STATE_EVENTS_CONSUMER,
        subject: str = STATE_EVENTS_SUBJECT,
        *,
        cluster_manager: NATSClusterManager | None = None,
        pool: asyncpg.Pool | None = None,
        sink: (
            Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None
        ) = None,
    ):
        self.stream = stream
        self.consumer_name = consumer_name
        self.subject = subject
        self._manager = cluster_manager or NATSClusterManager()
        self._pool = pool
        self._js: JetStreamContext | None = None
        self._sub = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._projection_ready = False
        self.metrics = StateProjectorMetrics()
        self._sink = sink

    async def start(self) -> None:
        if self._running:
            return

        await self._manager.connect()
        self._js = self._manager.jetstream
        await self._ensure_stream()
        await self._ensure_consumer()

        if self._sink is None:
            await self._ensure_projection_table()

        self._sub = await self._js.subscribe(
            self.subject,
            durable=self.consumer_name,
            stream=self.stream,
            manual_ack=True,
            ack_wait=STATE_EVENTS_ACK_WAIT_SECONDS,
            max_ack_pending=STATE_EVENTS_MAX_ACK_PENDING,
        )

        self._task = asyncio.create_task(self._consume())
        self._running = True
        logger.info("STATE_EVENTS durable consumer started")

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
        logger.info("STATE_EVENTS durable consumer stopped")

    async def _ensure_stream(self) -> None:
        if not self._js:
            return

        try:
            await self._js.stream_info(self.stream)
            return
        except Exception:
            logger.info("Creating STATE_EVENTS stream: %s", self.stream)

        await self._js.add_stream(
            name=self.stream,
            subjects=[self.subject],
            max_age=60 * 60 * 24 * 7,
            max_msgs=1_000_000,
            retention="limits",
            storage="file",
            description="Shared state event stream for projector recovery",
        )

    async def _ensure_consumer(self) -> None:
        if not self._js:
            return

        try:
            await self._js.consumer_info(self.stream, self.consumer_name)
            return
        except Exception:
            logger.info(
                "Creating durable consumer %s on %s", self.consumer_name, self.stream
            )

        await self._js.add_consumer(
            stream=self.stream,
            durable=self.consumer_name,
            ack_wait=STATE_EVENTS_ACK_WAIT_SECONDS,
            max_ack_pending=STATE_EVENTS_MAX_ACK_PENDING,
            ack_policy="explicit",
            # Replay original sequence to support guaranteed post-restart replay
            replay_policy="original",
            deliver_policy="all",
            filter_subject=self.subject,
        )

    async def _consume(self):
        try:
            async for msg in self._sub.messages:
                await self.process_message(msg)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("STATE_EVENTS consumer loop error: %s", exc)

    async def process_message(self, msg: Any) -> None:
        self.metrics.consumed_count += 1

        redeliveries = self._redelivery_count(msg)
        if redeliveries:
            self.metrics.replay_count += redeliveries

        try:
            payload = json.loads(msg.data.decode())
            await self._persist_state_event(payload)
            await self._ack(msg)
            self.metrics.acked_count += 1
            self.metrics.last_processed_ts = time.time()
        except Exception as exc:
            self.metrics.failure_count += 1
            self.metrics.last_error = str(exc)
            self.metrics.last_error_ts = time.time()
            await self._nak(msg)

    async def _persist_state_event(self, payload: dict[str, Any]) -> None:
        required = (
            "event_id",
            "gateway_id",
            "agent_id",
            "event_type",
            "entity_id",
            "ts",
            "version",
            "payload",
        )
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValueError(f"Invalid STATE_EVENT payload; missing keys: {missing}")

        if self._sink:
            await self._sink(payload)
            return

        if not self._pool:
            raise RuntimeError("No database pool available for state projection")

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {STATE_EVENTS_TABLE} (
                    event_id, gateway_id, agent_id, event_type, entity_id, ts,
                    version, payload, source_subject
                ) VALUES ($1, $2, $3, $4, $5, $6::timestamptz, $7, $8::jsonb, $9)
                ON CONFLICT (event_id) DO NOTHING
                """,
                payload["event_id"],
                payload["gateway_id"],
                payload["agent_id"],
                payload["event_type"],
                payload["entity_id"],
                payload["ts"],
                payload["version"],
                json.dumps(payload["payload"]),
                self.subject,
            )

    async def _ensure_projection_table(self) -> None:
        if self._projection_ready or not self._pool:
            return

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {STATE_EVENTS_TABLE} (
                    event_id UUID PRIMARY KEY,
                    gateway_id VARCHAR(64) NOT NULL,
                    agent_id VARCHAR(64),
                    event_type VARCHAR(64) NOT NULL,
                    entity_id VARCHAR(128) NOT NULL,
                    ts TIMESTAMPTZ NOT NULL,
                    version BIGINT NOT NULL DEFAULT 0,
                    payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    source_subject VARCHAR(128),
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        self._projection_ready = True

    async def _ack(self, msg: Any) -> None:
        try:
            result = msg.ack()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self.metrics.failure_count += 1
            self.metrics.last_error = str(exc)
            self.metrics.last_error_ts = time.time()
            try:
                result = msg.ack_sync()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass

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
            "source": "STATE_EVENTS",
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
                for key in (
                    "num_ack_pending",
                    "num_waiting",
                    "num_pending",
                    "num_delivery",
                ):
                    value = cinfo.get(key)
                    if isinstance(value, (int, float)):
                        return max(0, int(value))
            else:
                for key in (
                    "num_ack_pending",
                    "num_waiting",
                    "num_pending",
                    "num_delivery",
                ):
                    value = getattr(cinfo, key, None)
                    if isinstance(value, (int, float)):
                        return max(0, int(value))
        except Exception:
            pass

        return None
