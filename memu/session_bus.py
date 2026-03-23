"""Session-level event bus publisher for agent runners.

Provides a thin, async-safe publisher that emits structured SwarmEvents to the
NATS JetStream ``AGENT_EVENTS`` subject.  Every agent runner — regardless of
which session orchestration path it follows — should call::

    bus = SessionEventBus(agent_id="macklemore")
    async with bus:
        await bus.task_completed(task_id=..., result=..., tokens_used=500)
        await bus.memory_write(content="stored plan")
        await bus.decision(reasoning="chose path A because …")

The three convenience methods correspond to the three session lifecycle hooks
described in M1 of the agent backlog.

Environment variables:
    NATS_LOCAL_URL          Local NATS URL      (default: nats://localhost:4222)
    NATS_RAILWAY_URL        Railway NATS URL    (required outside Railway)
    NATS_RAILWAY_AUTH_TOKEN Optional Railway auth token
    NATS_LOCAL_AUTH_TOKEN   Optional local auth token
    NATS_AUTH_TOKEN         Back-compat fallback for Railway auth
    AGENT_EVENTS_SUBJECT    NATS subject name   (default: AGENT_EVENTS)
    SESSION_BUS_EMIT        Set "false" to disable all publishes (e.g. in tests)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from memu.cluster import NATSClusterManager
from memu.swarm_models import EventType, SwarmEvent

logger = logging.getLogger(__name__)

AGENT_EVENTS_SUBJECT = os.environ.get("AGENT_EVENTS_SUBJECT", "AGENT_EVENTS")
SESSION_BUS_EMIT = os.environ.get("SESSION_BUS_EMIT", "true").lower() != "false"

# Sentinel for optional task_id parameter
_MISSING = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_task_id() -> UUID:
    """Return a new UUID when the caller does not supply a task_id."""
    return uuid4()


class SessionEventBus:
    """Publish session-lifecycle events to the NATS AGENT_EVENTS subject.

    Usage (as async context manager)::

        async with SessionEventBus(agent_id="macklemore") as bus:
            await bus.task_completed(task_id=some_uuid, result={"ok": True})
            await bus.memory_write(content="remembered X")
            await bus.decision(reasoning="chose Y because Z")

    Or manually::

        bus = SessionEventBus(agent_id="macklemore")
        await bus.connect()
        try:
            await bus.task_completed(...)
        finally:
            await bus.close()
    """

    def __init__(
        self,
        agent_id: str,
        *,
        cluster_manager: NATSClusterManager | None = None,
        subject: str = AGENT_EVENTS_SUBJECT,
        emit: bool = SESSION_BUS_EMIT,
    ) -> None:
        self.agent_id = agent_id
        self.subject = subject
        self._emit = emit
        self._manager = cluster_manager or NATSClusterManager()
        self._connected = False

        # Lightweight publish metrics
        self.published_count = 0
        self.failure_count = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to NATS (idempotent)."""
        if self._connected:
            return
        await self._manager.connect()
        self._connected = True
        logger.debug("SessionEventBus connected for agent=%s", self.agent_id)

    async def close(self) -> None:
        """Disconnect from NATS."""
        if not self._connected:
            return
        await self._manager.close()
        self._connected = False
        logger.debug("SessionEventBus closed for agent=%s", self.agent_id)

    async def __aenter__(self) -> "SessionEventBus":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # High-level publisher hooks (the M1 API surface)
    # ------------------------------------------------------------------

    async def task_completed(
        self,
        *,
        task_id: UUID | str | None = None,
        result: dict[str, Any] | None = None,
        tokens_used: int = 0,
        duration_ms: int = 0,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Emit a ``task_completed`` event after a session task finishes.

        Args:
            task_id:      UUID of the completed task.  A new UUID is minted if omitted.
            result:       Arbitrary result payload (serialisable dict).
            tokens_used:  Token consumption for cost accounting.
            duration_ms:  Wall-clock duration of the task.
            error:        Set only when the task ended in a recoverable error.
            metadata:     Extra key/value pairs merged into the event payload.

        Returns:
            True if the event was successfully published (or emit is disabled).
        """
        event_type = EventType.TASK_FAILED if error else EventType.TASK_COMPLETED
        payload: dict[str, Any] = {
            "result": result or {},
            "tokens_used": tokens_used,
            "duration_ms": duration_ms,
        }
        if error:
            payload["error"] = error
        if metadata:
            payload.update(metadata)

        return await self._publish(
            event_type=event_type,
            task_id=self._coerce_uuid(task_id),
            payload=payload,
        )

    async def memory_write(
        self,
        content: str,
        *,
        task_id: UUID | str | None = None,
        namespace: str = "general",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Emit a ``checkpoint_saved`` event when the agent writes to memory.

        This covers any durable state write: memU writes, BACKLOG.md updates,
        daily memory files, or agent-level checkpoints.

        Args:
            content:    Human-readable description of what was written.
            task_id:    Associated task UUID (a new one is minted if omitted).
            namespace:  Semantic category (``decision``, ``fact``, ``plan``, …).
            metadata:   Extra key/value pairs merged into the event payload.
        """
        payload: dict[str, Any] = {
            "content_preview": content[:256],
            "namespace": namespace,
            "agent_id": self.agent_id,
        }
        if metadata:
            payload.update(metadata)

        return await self._publish(
            event_type=EventType.CHECKPOINT_SAVED,
            task_id=self._coerce_uuid(task_id),
            payload=payload,
        )

    async def decision(
        self,
        reasoning: str,
        *,
        task_id: UUID | str | None = None,
        chosen: str | None = None,
        alternatives: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Emit an ``audit_accepted`` event when the agent makes a key decision.

        Decisions are auditable inflection points — choosing a lane, selecting a
        model, approving a plan, etc.  They are published so other agents and the
        warden can observe the reasoning trace.

        Args:
            reasoning:    Human-readable explanation of why this path was chosen.
            task_id:      Associated task UUID.
            chosen:       The option that was selected.
            alternatives: Options that were considered but rejected.
            metadata:     Extra key/value pairs.
        """
        payload: dict[str, Any] = {
            "reasoning": reasoning[:1024],
            "agent_id": self.agent_id,
        }
        if chosen:
            payload["chosen"] = chosen
        if alternatives:
            payload["alternatives"] = alternatives
        if metadata:
            payload.update(metadata)

        return await self._publish(
            event_type=EventType.AUDIT_ACCEPTED,
            task_id=self._coerce_uuid(task_id),
            payload=payload,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _publish(
        self,
        *,
        event_type: EventType,
        task_id: UUID,
        payload: dict[str, Any],
    ) -> bool:
        """Build a SwarmEvent and publish it to NATS JetStream.

        Returns True on success, False on failure (never raises).
        """
        if not self._emit:
            logger.debug(
                "SESSION_BUS_EMIT=false — skipping %s for agent=%s",
                event_type.value,
                self.agent_id,
            )
            return True

        event = SwarmEvent(
            event_id=uuid4(),
            timestamp=_utcnow(),
            source_gateway=self.agent_id,
            event_type=event_type,
            task_id=task_id,
            payload=payload,
            compute_cost=payload.get("tokens_used", 0) * 0.000002,  # approx GPT-4 rate
        )

        raw = json.dumps(event.model_dump(mode="json"), default=str).encode()

        try:
            js = self._manager.jetstream
            ack = await js.publish(self.subject, raw)
            self.published_count += 1
            logger.info(
                "EVIDENCE: swarm.events published | agent=%s event_type=%s task_id=%s seq=%s",
                self.agent_id,
                event_type.value,
                task_id,
                getattr(ack, "seq", "?"),
            )
            return True

        except Exception as exc:
            self.failure_count += 1
            self._last_error = str(exc)
            logger.warning(
                "SessionEventBus publish failed | agent=%s event_type=%s error=%s",
                self.agent_id,
                event_type.value,
                exc,
            )
            return False

    @staticmethod
    def _coerce_uuid(value: UUID | str | None) -> UUID:
        if value is None:
            return _new_task_id()
        if isinstance(value, UUID):
            return value
        return UUID(str(value))

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        """Return a snapshot of publish metrics for health reporting."""
        return {
            "agent_id": self.agent_id,
            "published_count": self.published_count,
            "failure_count": self.failure_count,
            "last_error": self._last_error,
            "connected": self._connected,
        }


# ---------------------------------------------------------------------------
# Default session orchestration hooks
# ---------------------------------------------------------------------------
# The functions below are the *default* integration points.  Any agent runner
# that does not implement its own bus lifecycle should call these wrappers.
# They create an ephemeral bus, publish one event, and disconnect — fire and
# forget style.  For high-throughput sessions, prefer the context-manager API.

async def _fire_and_forget(
    agent_id: str,
    event_type: EventType,
    payload: dict[str, Any],
    task_id: UUID | None = None,
) -> bool:
    """Connect, publish one event, disconnect.  Best-effort — never raises."""
    bus = SessionEventBus(agent_id=agent_id)
    try:
        await bus.connect()
        return await bus._publish(
            event_type=event_type,
            task_id=task_id or _new_task_id(),
            payload=payload,
        )
    except Exception as exc:
        logger.warning("fire_and_forget error: %s", exc)
        return False
    finally:
        try:
            await bus.close()
        except Exception:
            pass


async def hook_session_task_completed(
    agent_id: str,
    task_id: UUID | str | None = None,
    *,
    result: dict[str, Any] | None = None,
    tokens_used: int = 0,
    duration_ms: int = 0,
) -> bool:
    """Default session orchestration hook: task completed.

    Call this after any session-level task finishes in the default path::

        ok = await hook_session_task_completed("macklemore", task_id, tokens_used=300)
    """
    tid = SessionEventBus._coerce_uuid(task_id)
    return await _fire_and_forget(
        agent_id=agent_id,
        event_type=EventType.TASK_COMPLETED,
        payload={"result": result or {}, "tokens_used": tokens_used, "duration_ms": duration_ms},
        task_id=tid,
    )


async def hook_session_memory_write(
    agent_id: str,
    content: str,
    task_id: UUID | str | None = None,
    *,
    namespace: str = "general",
) -> bool:
    """Default session orchestration hook: memory/checkpoint write."""
    tid = SessionEventBus._coerce_uuid(task_id)
    return await _fire_and_forget(
        agent_id=agent_id,
        event_type=EventType.CHECKPOINT_SAVED,
        payload={"content_preview": content[:256], "namespace": namespace, "agent_id": agent_id},
        task_id=tid,
    )


async def hook_session_decision(
    agent_id: str,
    reasoning: str,
    task_id: UUID | str | None = None,
    *,
    chosen: str | None = None,
) -> bool:
    """Default session orchestration hook: agent decision."""
    tid = SessionEventBus._coerce_uuid(task_id)
    payload: dict[str, Any] = {"reasoning": reasoning[:1024], "agent_id": agent_id}
    if chosen:
        payload["chosen"] = chosen
    return await _fire_and_forget(
        agent_id=agent_id,
        event_type=EventType.AUDIT_ACCEPTED,
        payload=payload,
        task_id=tid,
    )
