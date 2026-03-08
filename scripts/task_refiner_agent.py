#!/usr/bin/env python3
"""Task refining agent for the unified registry.

Reads pending tasks, enforces strict acceptance criteria (specific + measurable +
traceable), updates refine/review states, and emits NATS events on rejection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone

import asyncpg

from memu.nats_publisher import NATSEventPublisher
from memu.cluster import NATSClusterManager


def qualifies_for_review(task_text: str) -> tuple[bool, str]:
    task_text = task_text.lower()

    action_verbs = [
        "fix",
        "implement",
        "add",
        "create",
        "remove",
        "update",
        "refactor",
        "patch",
        "investigate",
        "adjust",
        "document",
        "test",
        "deploy",
    ]
    measurable_patterns = [
        r"\b(\d+%?|\d+\s*/\s*\d+)\b",
        r"\b(file|files|endpoint|api|route|migration|schema|config|table|service|module)\b",
        r"\b(evidence|proof|assert|assertion|checklist|diff|commit)\b",
    ]

    has_action = any(v in task_text for v in action_verbs)
    if not has_action:
        return False, "No action verb (fix/add/update/test/etc) found"

    has_measurable = any(re.search(pattern, task_text) for pattern in measurable_patterns)
    if not has_measurable:
        return False, "No measurable artifact/metric found in task text"

    if len(task_text) < 50:
        return False, "Task too short for reliable execution"

    return True, "approved"


async def refine_pending_tasks(db_url: str, publish_nats: bool, limit: int, owner_filter: str | None) -> int:
    conn = await asyncpg.connect(db_url)
    publisher: NATSEventPublisher | None = None

    try:
        if publish_nats:
            try:
                cluster = NATSClusterManager()
                await cluster.connect()
                publisher = NATSEventPublisher(cluster, gateway_id="task-refiner-agent")
            except Exception:
                publisher = None

        where = ["refine_status = 'pending'"]
        params: list[object] = []
        if owner_filter:
            where.append(f"owner_id = ${len(params) + 1}")
            params.append(owner_filter)

        query = (
            "SELECT id, task, metadata, source, owner_id "
            f"FROM backlog WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT {int(limit)}"
        )

        rows = await conn.fetch(query, *params)
        reviewed = 0

        for row in rows:
            task_id = row["id"]
            task_text = (row["task"] or "")
            metadata = row["metadata"] or {}
            ok, reason = qualifies_for_review(task_text)

            if ok:
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["refined_at"] = datetime.now(timezone.utc).isoformat()
                await conn.execute(
                    """
                    UPDATE backlog
                    SET
                        refine_status = 'approved',
                        review_status = COALESCE(review_status, 'pending_review'),
                        metadata = $2::jsonb,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    task_id,
                    json.dumps(metadata),
                )
            else:
                if not isinstance(metadata, dict):
                    metadata = {}
                existing_notes = metadata.get("review_notes", "")
                entry = f"[refiner:{datetime.now(timezone.utc).isoformat()}] reason={reason}"
                metadata["review_notes"] = "\n".join([n for n in [existing_notes, entry] if n])

                await conn.execute(
                    """
                    UPDATE backlog
                    SET
                        refine_status = 'needs_revision',
                        review_status = 'needs_input',
                        status = 'pending',
                        retry_count = COALESCE(retry_count, 0) + 1,
                        metadata = $2::jsonb,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    task_id,
                    json.dumps(metadata),
                )

                if publisher:
                    await publisher.publish_task_failed(
                        agent_id=row["owner_id"] or "system",
                        task_id=str(task_id),
                        title=task_text[:256],
                        reason=f"refine_failed:{reason}",
                        source=row["source"] or "refiner",
                        evidence=str(metadata),
                    )

            reviewed += 1

        return reviewed
    finally:
        await conn.close()
        if publisher and getattr(publisher, "cluster", None):
            await publisher.cluster.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run task refiner agent")
    parser.add_argument("--db-url", type=str, default=None)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--owner", type=str, default=None)
    parser.add_argument("--publish-nats", action="store_true")
    args = parser.parse_args()

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL missing")

    import asyncio

    count = asyncio.run(refine_pending_tasks(db_url, args.publish_nats, args.limit, args.owner))
    print(f"refinement_runs={count}")


if __name__ == "__main__":
    main()
