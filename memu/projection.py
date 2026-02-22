"""DAG Projection Engine — Event Sourcing state reconstructor.

Reads the append-only event ledger chronologically and reconstructs
the current Directed Acyclic Graph (DAG) of tasks in memory.

This is the core of the event sourcing pattern:
- The events table is the source of truth (immutable)
- The tasks table is a materialized projection (rebuildable)
- This module can reconstruct the full DAG from scratch at any time
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from memu.swarm_models import (
    EventType,
    TaskNode,
    TaskStatus,
    ComputeBudget,
)


async def project_task_dag(
    pool: asyncpg.Pool,
    root_prompt_id: UUID,
) -> dict[UUID, TaskNode]:
    """Reconstruct the full DAG for a root prompt by replaying events.

    Returns a dict mapping task_id -> TaskNode with all relationships resolved.
    """
    async with pool.acquire() as conn:
        # Fetch all events for tasks belonging to this root prompt, ordered chronologically
        rows = await conn.fetch(
            """
            SELECT e.event_id, e.timestamp, e.task_id, e.gateway_id,
                   e.event_type, e.payload, e.compute_cost
            FROM events e
            JOIN tasks t ON e.task_id = t.task_id
            WHERE t.root_prompt_id = $1
            ORDER BY e.timestamp ASC
            """,
            root_prompt_id,
        )

    nodes: dict[UUID, TaskNode] = {}

    for row in rows:
        task_id: UUID = row["task_id"]
        event_type = row["event_type"]
        payload: dict[str, Any] = row["payload"] or {}
        cost: float = row["compute_cost"] or 0.0

        # Initialize node if first event
        if task_id not in nodes:
            nodes[task_id] = TaskNode(
                task_id=task_id,
                root_prompt_id=root_prompt_id,
                title=payload.get("title", "Unknown"),
                created_at=row["timestamp"],
            )

        node = nodes[task_id]
        node.events.append(row["event_id"])
        node.compute_spent += cost

        # Apply event to node state
        match event_type:
            case EventType.TASK_DRAFTED.value:
                node.title = payload.get("title", node.title)
                node.status = TaskStatus.PENDING
                node.parent_task_id = (
                    UUID(payload["parent_task_id"])
                    if payload.get("parent_task_id")
                    else None
                )
                node.compute_budget = payload.get("compute_budget", 0.0)
                # Register as child of parent
                if node.parent_task_id and node.parent_task_id in nodes:
                    parent = nodes[node.parent_task_id]
                    if task_id not in parent.children:
                        parent.children.append(task_id)

            case EventType.BID_SUBMITTED.value:
                node.status = TaskStatus.BIDDING

            case EventType.LEASE_GRANTED.value:
                node.status = TaskStatus.CLAIMED
                node.assigned_gateway = row["gateway_id"]

            case EventType.TASK_CLAIMED.value:
                node.status = TaskStatus.EXECUTING
                node.assigned_gateway = payload.get("gateway_id", row["gateway_id"])

            case EventType.TASK_COMPLETED.value:
                node.status = TaskStatus.COMPLETED
                node.completed_at = row["timestamp"]

            case EventType.TASK_FAILED.value:
                node.status = TaskStatus.FAILED

            case EventType.TASK_CANCELLED.value:
                node.status = TaskStatus.CANCELLED

            case EventType.TASK_AMENDED.value:
                # Amendment accepted — update title/description
                if "title" in payload:
                    node.title = payload["title"]

            case EventType.AUDIT_PROPOSED.value:
                node.status = TaskStatus.AUDIT_PENDING

            case EventType.AUDIT_ACCEPTED.value:
                # Revert to completed after audit passes
                node.status = TaskStatus.COMPLETED

            case EventType.AUDIT_REJECTED.value:
                # Task needs re-execution
                node.status = TaskStatus.PENDING
                node.assigned_gateway = None

            case EventType.LEASE_EXPIRED.value:
                node.status = TaskStatus.PENDING
                node.assigned_gateway = None

            case EventType.CIRCUIT_BREAKER.value:
                node.status = TaskStatus.FAILED

    # Second pass: resolve parent-child edges for nodes created out of order
    for task_id, node in nodes.items():
        if node.parent_task_id and node.parent_task_id in nodes:
            parent = nodes[node.parent_task_id]
            if task_id not in parent.children:
                parent.children.append(task_id)

    return nodes


async def get_compute_budget(
    pool: asyncpg.Pool,
    root_prompt_id: UUID,
) -> ComputeBudget:
    """Calculate total compute spent across all tasks for a root prompt."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(e.compute_cost), 0) AS total_cost,
                COUNT(e.event_id) AS total_events,
                rp.metadata->>'max_cost_usd' AS max_cost,
                rp.metadata->>'max_tokens' AS max_tokens
            FROM events e
            JOIN tasks t ON e.task_id = t.task_id
            JOIN root_prompts rp ON t.root_prompt_id = rp.id
            WHERE t.root_prompt_id = $1
            """,
            root_prompt_id,
        )

    return ComputeBudget(
        root_prompt_id=root_prompt_id,
        max_cost_usd=float(row["max_cost"] or 1.0),
        max_tokens=int(row["max_tokens"] or 50_000),
        cost_spent_usd=float(row["total_cost"]),
        tokens_spent=0,  # TODO: track token-level granularity in events
    )


async def materialize_tasks(
    pool: asyncpg.Pool,
    root_prompt_id: UUID,
) -> None:
    """Rebuild the tasks table from events (full re-projection).

    Use this to recover from corruption or verify consistency.
    The events table is ALWAYS the source of truth.
    """
    dag = await project_task_dag(pool, root_prompt_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            for task_id, node in dag.items():
                await conn.execute(
                    """
                    INSERT INTO tasks (task_id, root_prompt_id, parent_task_id, title,
                                       status, assigned_gateway, compute_budget,
                                       compute_spent, created_at, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
                    ON CONFLICT (task_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        assigned_gateway = EXCLUDED.assigned_gateway,
                        compute_spent = EXCLUDED.compute_spent,
                        updated_at = NOW()
                    """,
                    node.task_id,
                    node.root_prompt_id,
                    node.parent_task_id,
                    node.title,
                    node.status.value,
                    node.assigned_gateway,
                    node.compute_budget,
                    node.compute_spent,
                    node.created_at,
                )
