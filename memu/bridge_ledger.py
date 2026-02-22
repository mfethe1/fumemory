"""In-memory event ledger and DAG projection for the WebSocket bridge.

Since the bridge may not have direct Postgres access, it maintains
its own in-memory event log + projected DAG from NATS events.
This enables the Glass Box UI to render the full DAG without
a database connection.

This is intentionally a cache/projection — Postgres remains the
authoritative event store when available.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Maximum events to keep in memory (ring buffer behavior)
MAX_EVENTS = 10_000


@dataclass
class InMemoryTask:
    """Projected task state from events."""
    task_id: str
    title: str = "Unknown"
    status: str = "pending"
    parent_task_id: str | None = None
    root_prompt_id: str | None = None
    assigned_gateway: str | None = None
    compute_budget: float = 0.0
    compute_spent: float = 0.0
    children: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    created_at: str | None = None
    completed_at: str | None = None
    last_updated: str | None = None


class BridgeLedger:
    """In-memory event ledger with DAG projection for the bridge.

    Processes swarm events and maintains a live DAG that the
    Glass Box UI can query via REST endpoints.
    """

    def __init__(self, max_events: int = MAX_EVENTS):
        self.max_events = max_events
        self.events: list[dict[str, Any]] = []
        self.tasks: dict[str, InMemoryTask] = {}
        self.root_prompts: set[str] = set()
        self.lane_locks: dict[str, dict[str, Any]] = {}
        self.dlq_entries: list[dict[str, Any]] = []
        self.gateway_heartbeats: dict[str, dict[str, Any]] = {}

    def process_event(self, raw: dict[str, Any]) -> InMemoryTask | None:
        """Process a raw event dict and update the projection.

        Returns the affected task (if any), or None.
        """
        # Store raw event
        self.events.append(raw)
        if len(self.events) > self.max_events:
            self.events = self.events[-self.max_events:]

        event_type = raw.get("event_type", "")
        task_id = raw.get("task_id", "")
        payload = raw.get("payload", {})
        cost = raw.get("compute_cost", 0.0) or 0.0
        source = raw.get("source_gateway", "unknown")
        ts = raw.get("timestamp", datetime.now(timezone.utc).isoformat())

        if not task_id:
            # Non-task events (heartbeats, lane locks, etc.)
            self._process_system_event(event_type, payload, source, ts)
            return None

        # Get or create task
        if task_id not in self.tasks:
            self.tasks[task_id] = InMemoryTask(
                task_id=task_id,
                created_at=ts,
            )

        task = self.tasks[task_id]
        task.events.append(raw.get("event_id", ""))
        task.compute_spent += cost
        task.last_updated = ts

        # Apply event
        match event_type:
            case "task_drafted":
                task.title = payload.get("title", task.title)
                task.status = "pending"
                task.root_prompt_id = payload.get("root_prompt_id")
                task.parent_task_id = payload.get("parent_task_id")
                task.compute_budget = payload.get("compute_budget", 0.0)
                if task.root_prompt_id:
                    self.root_prompts.add(task.root_prompt_id)
                # Link to parent
                if task.parent_task_id and task.parent_task_id in self.tasks:
                    parent = self.tasks[task.parent_task_id]
                    if task_id not in parent.children:
                        parent.children.append(task_id)

            case "bid_submitted":
                task.status = "bidding"

            case "lease_granted":
                task.status = "claimed"
                task.assigned_gateway = payload.get("gateway_id", source)

            case "task_claimed":
                task.status = "executing"
                task.assigned_gateway = payload.get("gateway_id", source)

            case "task_completed":
                task.status = "completed"
                task.completed_at = ts

            case "task_failed":
                task.status = "failed"

            case "task_cancelled":
                task.status = "cancelled"

            case "task_amended":
                if "title" in payload:
                    task.title = payload["title"]

            case "audit_proposed":
                task.status = "audit_pending"

            case "audit_accepted":
                task.status = "completed"

            case "audit_rejected":
                task.status = "pending"
                task.assigned_gateway = None

            case "lease_expired":
                task.status = "pending"
                task.assigned_gateway = None

            case "circuit_breaker":
                task.status = "failed"

            case "system_halt":
                # Mark all executing tasks as halted
                for t in self.tasks.values():
                    if t.status == "executing":
                        t.status = "halted"

        # Second pass: resolve parent references
        if task.parent_task_id and task.parent_task_id in self.tasks:
            parent = self.tasks[task.parent_task_id]
            if task_id not in parent.children:
                parent.children.append(task_id)

        return task

    def _process_system_event(
        self, event_type: str, payload: dict, source: str, ts: str
    ):
        """Handle non-task system events."""
        match event_type:
            case "heartbeat_ping":
                self.gateway_heartbeats[source] = {
                    "gateway_id": source,
                    "last_seen": ts,
                    "payload": payload,
                }

            case "lane_contested" | "lane_acquired":
                lane = payload.get("lane", "unknown")
                self.lane_locks[lane] = {
                    "lane": lane,
                    "holder": payload.get("gateway_id", source),
                    "event": event_type,
                    "timestamp": ts,
                }

            case "dlq_entry":
                self.dlq_entries.append({
                    "source": source,
                    "payload": payload,
                    "timestamp": ts,
                })

    def get_dag(self, root_prompt_id: str | None = None) -> dict[str, Any]:
        """Return the full DAG or a filtered view by root prompt.

        Returns a structure the Glass Box UI can directly render.
        """
        tasks = self.tasks.values()
        if root_prompt_id:
            tasks = [t for t in tasks if t.root_prompt_id == root_prompt_id]

        nodes = []
        edges = []

        for task in tasks:
            nodes.append({
                "id": task.task_id,
                "data": {
                    "label": task.title,
                    "status": task.status,
                    "gateway": task.assigned_gateway,
                    "computeBudget": task.compute_budget,
                    "computeSpent": task.compute_spent,
                    "eventCount": len(task.events),
                },
                "type": "taskNode",
            })

            if task.parent_task_id:
                edges.append({
                    "id": f"e-{task.parent_task_id}-{task.task_id}",
                    "source": task.parent_task_id,
                    "target": task.task_id,
                })

        # Compute budget totals
        total_budget = sum(t.compute_budget for t in self.tasks.values())
        total_spent = sum(t.compute_spent for t in self.tasks.values())

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "total_tasks": len(self.tasks),
                "total_events": len(self.events),
                "root_prompts": list(self.root_prompts),
                "compute_budget": total_budget,
                "compute_spent": total_spent,
                "gateways_active": len(self.gateway_heartbeats),
                "lane_locks": self.lane_locks,
                "dlq_count": len(self.dlq_entries),
            },
        }

    def get_event_log(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """Return recent events (newest first)."""
        events = list(reversed(self.events))
        return events[offset : offset + limit]

    def get_trajectory(self, task_id: str) -> dict[str, Any]:
        """Return the full event trajectory for a specific task."""
        task = self.tasks.get(task_id)
        if not task:
            return {"error": f"Task {task_id} not found"}

        # Find all events for this task
        task_events = [
            e for e in self.events if e.get("task_id") == task_id
        ]

        # Find children and their events (depth 1)
        child_events = []
        for child_id in task.children:
            child_events.extend(
                e for e in self.events if e.get("task_id") == child_id
            )

        return {
            "task_id": task_id,
            "title": task.title,
            "status": task.status,
            "assigned_gateway": task.assigned_gateway,
            "compute_budget": task.compute_budget,
            "compute_spent": task.compute_spent,
            "events": task_events,
            "children": [
                {
                    "task_id": cid,
                    "title": self.tasks[cid].title if cid in self.tasks else "?",
                    "status": self.tasks[cid].status if cid in self.tasks else "?",
                }
                for cid in task.children
            ],
            "parent_task_id": task.parent_task_id,
            "root_prompt_id": task.root_prompt_id,
        }
