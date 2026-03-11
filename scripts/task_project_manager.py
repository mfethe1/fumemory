#!/usr/bin/env python3
"""Project Manager Agent (Lead Agent Planning Phase).

Derived from the DeerFlow v2.0 'Project Manager and Task Breakout' pattern.
This agent acts as the Lead Planner. It scans the backlog for large, ambiguous,
or complex tasks (e.g. ones that failed refinement or are explicitly marked
for planning), breaks them down into specific, measurable sub-tasks, and
inserts them back into the registry for the swarm to execute in parallel.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import asyncpg

from memu.nats_publisher import NATSEventPublisher
from memu.cluster import NATSClusterManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("task_project_manager")


def break_down_task(task_text: str, task_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Heuristic logic to break down a complex task into sub-tasks.
    
    In a full LLM pipeline, this would call an OpenAI/Anthropic endpoint
    to intelligently decompose the task. For the immediate gateway swarm
    pattern, we apply structural rules to split it.
    """
    sub_tasks = []
    
    # Simple heuristic: split by newlines, bullets, or sentences if very long
    lines = [line.strip() for line in task_text.split("\n") if line.strip() and len(line.strip()) > 10]
    
    if len(lines) > 1 and any(line.startswith("-") or line.startswith("*") for line in lines):
        # Already somewhat itemized
        for i, line in enumerate(lines):
            clean_line = line.lstrip("-* ").strip()
            if clean_line:
                sub_tasks.append({
                    "task": f"Phase {i+1}: {clean_line}",
                    "priority": "P2",
                    "lane": "execution"
                })
    else:
        # If it's a monolithic block, break into standard agent phases
        sub_tasks = [
            {
                "task": f"Phase 1 (Research): Analyze requirements and gather context for '{task_text[:50]}...'",
                "priority": "P1",
                "lane": "research"
            },
            {
                "task": f"Phase 2 (Implementation): Execute core logic for '{task_text[:50]}...'",
                "priority": "P2",
                "lane": "execution"
            },
            {
                "task": f"Phase 3 (Validation): Add tests and QA for '{task_text[:50]}...'",
                "priority": "P2",
                "lane": "qa"
            }
        ]
        
    return sub_tasks


async def process_planning_queue(db_url: str, publish_nats: bool, limit: int) -> int:
    """Finds tasks requiring project management breakdown and processes them."""
    conn = await asyncpg.connect(db_url)
    publisher: NATSEventPublisher | None = None

    try:
        if publish_nats:
            try:
                cluster = NATSClusterManager()
                await cluster.connect()
                publisher = NATSEventPublisher(cluster, gateway_id="project-manager")
            except Exception as e:
                logger.warning(f"Failed to connect to NATS: {e}")
                publisher = None

        # Look for tasks that are either explicitly marked for planning or failed refinement due to ambiguity
        query = """
            SELECT id, task, metadata, source, owner_id, tenant_id, project
            FROM backlog 
            WHERE status = 'pending'
              AND (
                review_status = 'planning_required' 
                OR (refine_status = 'needs_revision' AND task ILIKE '%build%')
              )
            ORDER BY created_at ASC 
            LIMIT $1
        """
        rows = await conn.fetch(query, limit)
        processed = 0

        for row in rows:
            task_id = row["id"]
            task_text = row["task"] or ""
            metadata = row["metadata"] or {}
            tenant_id = row["tenant_id"]
            project = row["project"]

            logger.info(f"Project Manager breaking down task: {task_id}")
            
            sub_tasks = break_down_task(task_text, metadata)
            
            if not sub_tasks:
                logger.warning(f"Could not break down task {task_id}")
                continue
                
            async with conn.transaction():
                # 1. Insert sub-tasks
                for i, st in enumerate(sub_tasks):
                    st_meta = {
                        "parent_task_id": str(task_id),
                        "breakout_index": i,
                        "generated_by": "project_manager",
                        "deerflow_pattern": "strict_sub_agent_isolation"
                    }
                    
                    new_id = await conn.fetchval(
                        """
                        INSERT INTO backlog (
                            task, priority, owner_id, lane, metadata, 
                            tenant_id, status, refine_status, source, project
                        ) VALUES (
                            $1, $2, $3, $4, $5, 
                            $6, 'pending', 'pending', 'project_manager', $7
                        ) RETURNING id
                        """,
                        st["task"],
                        st["priority"],
                        None, # Unassigned, let swarm claim
                        st["lane"],
                        json.dumps(st_meta),
                        tenant_id,
                        project
                    )
                    
                    if publisher:
                        # Emits event without full context bleed to enforce Shallow Orchestration
                        await publisher.publish_task_created(
                            task_id=str(new_id),
                            title=st["task"][:128],
                            lane=st["lane"],
                            priority=st["priority"]
                        )
                
                # 2. Mark parent task as broken out / delegated
                metadata["planning_completed_at"] = datetime.now(timezone.utc).isoformat()
                metadata["sub_tasks_generated"] = len(sub_tasks)
                
                await conn.execute(
                    """
                    UPDATE backlog
                    SET
                        status = 'delegated',
                        review_status = 'planned',
                        metadata = $2::jsonb,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    task_id,
                    json.dumps(metadata)
                )

            processed += 1

        return processed

    finally:
        await conn.close()
        if publisher and getattr(publisher, "cluster", None):
            await publisher.cluster.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Project Manager (Lead Agent Planner)")
    parser.add_argument("--db-url", type=str, default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--publish-nats", action="store_true", default=True)
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        logger.warning("DATABASE_URL missing. Skipping execution.")
        return

    count = asyncio.run(process_planning_queue(db_url, args.publish_nats, args.limit))
    logger.info(f"Project Manager successfully broke down {count} tasks.")


if __name__ == "__main__":
    main()
