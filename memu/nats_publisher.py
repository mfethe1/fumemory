"""NATS Event Publisher — wires real agent actions into the swarm mesh.

This is the missing link: it takes concrete agent actions (memory writes,
task claims, searches, decisions) and publishes them as validated SwarmEvents
onto the NATS mesh so every subscriber (event_consumer, ws_bridge, projector)
sees real traffic instead of silence.

Usage:
    publisher = NATSEventPublisher(cluster_manager)
    await publisher.publish_memory_written(agent_id="lenny", memory_id="...", content="...")
    await publisher.publish_task_claimed(agent_id="lenny", task_id="...", title="...")
    await publisher.publish_search_logged(agent_id="lenny", query="...", source="brave")
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from memu.cluster import NATSClusterManager
from memu.crypto import GatewayKeyPair, canonical_event_hash, get_backend
from memu.swarm_models import EventType, SwarmEvent

logger = logging.getLogger(__name__)


class NATSEventPublisher:
    """Publish validated and cryptographically signed events onto the NATS swarm mesh."""

    def __init__(
        self,
        cluster: NATSClusterManager,
        gateway_id: str = "memu-api",
        keypair: GatewayKeyPair | None = None,
    ):
        self.cluster = cluster
        self.gateway_id = gateway_id
        # Generate keypair if not provided and crypto backend is available
        if keypair is not None:
            self.keypair = keypair
        elif get_backend() != "none":
            self.keypair = GatewayKeyPair()
            logger.info(
                f"Gateway keypair generated (backend={get_backend()}, "
                f"pubkey={self.keypair.public_key_hex[:16]}...)"
            )
        else:
            self.keypair = None
            logger.warning("No crypto backend — events will be published unsigned")

    @property
    def public_key_hex(self) -> str:
        """Public key for registration in gateway_registry."""
        return self.keypair.public_key_hex if self.keypair else ""

    async def _publish(self, subject: str, event: SwarmEvent) -> bool:
        """Publish a single event, return True on success."""
        try:
            nc = self.cluster.active_connection
            data = event.model_dump_json().encode()
            await nc.publish(subject, data)
            logger.info(f"Published {event.event_type.value} to {subject}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish {event.event_type.value}: {e}")
            return False

    def _sign_event(self, event: SwarmEvent) -> str | None:
        """Sign an event and return the hex signature."""
        if not self.keypair:
            return None
        payload_hash = canonical_event_hash(
            task_id=event.task_id,
            timestamp=event.timestamp,
            event_type=event.event_type.value,
            payload=event.payload,
        )
        return self.keypair.sign(payload_hash)

    def _make_event(
        self,
        event_type: EventType,
        task_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SwarmEvent:
        event = SwarmEvent(
            source_gateway=self.gateway_id,
            event_type=event_type,
            task_id=task_id or uuid4(),
            payload=payload or {},
        )
        # Sign the event before publishing
        event.signature = self._sign_event(event)
        return event

    # --- Concrete event publishers ---

    async def publish_memory_written(
        self,
        agent_id: str,
        memory_id: str,
        content: str,
        memory_type: str = "fact",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Fired after every successful memory write."""
        event = self._make_event(
            EventType.MEMORY_WRITTEN,
            payload={
                "agent_id": agent_id,
                "memory_id": memory_id,
                "content_preview": content[:200],
                "memory_type": memory_type,
                "metadata": metadata or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await self._publish(f"swarm.memory.{agent_id}", event)

    async def publish_task_drafted(
        self,
        agent_id: str,
        task_id: str,
        title: str,
        source: str = "notion",
        risk_score: int = 25,
        project: str | None = None,
    ) -> bool:
        """Fired when a new task is drafted into the unified task registry."""
        event = self._make_event(
            EventType.TASK_DRAFTED,
            payload={
                "agent_id": agent_id,
                "task_id": task_id,
                "title": title[:256],
                "source": source,
                "risk_score": risk_score,
                "project": project,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await self._publish("swarm.task.drafted", event)

    async def publish_task_claimed(
        self,
        agent_id: str,
        task_id: str,
        title: str,
        source: str = "notion",
    ) -> bool:
        """Fired when an agent claims a task."""
        event = self._make_event(
            EventType.TASK_CLAIMED,
            payload={
                "agent_id": agent_id,
                "task_id": task_id,
                "title": title,
                "source": source,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await self._publish(f"swarm.task.claimed", event)

    async def publish_task_failed(
        self,
        agent_id: str,
        task_id: str,
        title: str,
        reason: str = "failed",
        source: str = "notion",
        evidence: str = "",
    ) -> bool:
        """Fired when a task fails review or completion criteria are not met."""
        event = self._make_event(
            EventType.TASK_FAILED,
            payload={
                "agent_id": agent_id,
                "task_id": task_id,
                "title": title[:256],
                "reason": reason,
                "evidence": evidence[:500],
                "source": source,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await self._publish("swarm.task.failed", event)

    async def publish_task_completed(
        self,
        agent_id: str,
        task_id: str,
        title: str,
        evidence: str = "",
        source: str = "notion",
    ) -> bool:
        """Fired when an agent completes a task."""
        event = self._make_event(
            EventType.TASK_COMPLETED,
            payload={
                "agent_id": agent_id,
                "task_id": task_id,
                "title": title,
                "evidence": evidence[:500],
                "source": source,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await self._publish(f"swarm.task.completed", event)

    async def publish_search_logged(
        self,
        agent_id: str,
        query: str,
        source: str = "brave",
        result_count: int = 0,
        search_id: str | None = None,
    ) -> bool:
        """Fired when a web search is stored in the search cache."""
        event = self._make_event(
            EventType.DECISION_MADE,  # Reusing closest event type
            payload={
                "action": "search_logged",
                "agent_id": agent_id,
                "query": query,
                "source": source,
                "result_count": result_count,
                "search_id": search_id or str(uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await self._publish(f"swarm.search.{agent_id}", event)

    async def publish_heartbeat(
        self,
        agent_id: str,
        status: str = "active",
        current_task: str | None = None,
    ) -> bool:
        """Agent heartbeat — proves the agent is alive and what it's doing."""
        event = self._make_event(
            EventType.HEARTBEAT,
            payload={
                "agent_id": agent_id,
                "status": status,
                "current_task": current_task,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await self._publish(f"swarm.heartbeat.{agent_id}", event)

    async def publish_action_logged(
        self,
        agent_id: str,
        action_type: str,
        description: str,
        task_id: str | None = None,
        outcome: str = "success",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Generic action audit log — the strict mandate logger."""
        event = self._make_event(
            EventType.DECISION_MADE,
            payload={
                "action": "action_logged",
                "agent_id": agent_id,
                "action_type": action_type,
                "description": description[:500],
                "task_id": task_id,
                "outcome": outcome,
                "metadata": metadata or {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        return await self._publish(f"swarm.actions.{agent_id}", event)
