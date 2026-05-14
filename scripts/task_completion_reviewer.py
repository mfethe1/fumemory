#!/usr/bin/env python3
"""Critical completion reviewer for unified tasks.

Given structured agent completion payload, this script applies strict validation for
objective outcomes before task closure. If checks fail, task stays open and is
re-queued with reviewer guidance.
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


MEASURABLE_PATTERNS = [
    r"\b(\d+%?|\d+\s*/\s*\d+)\b",
    r"\b(file|files|endpoint|api|route|migration|schema|config|table|service|module|coverage|latency|p95|sec|ms|error rate)\b",
    r"\b(test|tested|passed|coverage|assert|evidence|proof|diff|commit|pr)\b",
]


def validate_completion(payload: dict) -> tuple[bool, str]:
    outcome = str(payload.get("outcome", "")).strip()
    evidence = str(payload.get("evidence", "")).strip()
    files_touched = payload.get("files_touched", []) or []
    tests = payload.get("tests", []) or []

    if not outcome:
        return False, "Missing outcome statement"
    if len(outcome) < 20:
        return False, "Outcome too short / unmeasurable"

    if not evidence:
        return False, "Missing evidence details"
    if len(evidence) < 20:
        return False, "Evidence summary too short"

    has_files = isinstance(files_touched, list) and len(files_touched) > 0
    has_tests = isinstance(tests, list) and len(tests) > 0
    measurable_text = (outcome + " " + evidence).lower()
    has_measurable = bool(
        has_files
        or has_tests
        or any(re.search(pattern, measurable_text) for pattern in MEASURABLE_PATTERNS)
    )

    if not has_measurable:
        return False, "No measurable artifact referenced"

    return True, "passed"


async def review_completion(db_url: str, task_id: str, payload: dict, reviewer_id: str, publish_nats: bool) -> str:
    conn = await asyncpg.connect(db_url)
    publisher: NATSEventPublisher | None = None

    try:
        if publish_nats:
            try:
                cluster = NATSClusterManager()
                await cluster.connect()
                publisher = NATSEventPublisher(cluster, gateway_id="task-completion-reviewer")
            except Exception:
                publisher = None

        row = await conn.fetchrow("SELECT * FROM backlog WHERE id = $1", task_id)
        if not row:
            return "not_found"

        ok, reason = validate_completion(payload)

        existing_notes = row["review_notes"] or ""
        new_entry = (
            f"[{datetime.now(timezone.utc).isoformat()}] reviewer={reviewer_id} "
            f"decision={'approve' if ok else 'rework'} reason={reason}"
        )
        notes = "\n".join([n for n in [existing_notes, new_entry] if n])

        if ok:
            await conn.fetchrow(
                """
                UPDATE backlog
                SET
                    status = 'done',
                    review_status = 'passed',
                    reviewer_id = $2,
                    reviewed_at = NOW(),
                    review_notes = $3,
                    evidence = $4,
                    completion_criteria = CASE
                        WHEN completion_criteria IS NULL OR completion_criteria = ''
                        THEN $5
                        ELSE completion_criteria
                    END,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                task_id,
                reviewer_id,
                notes,
                str(payload.get("evidence") or payload.get("details") or ""),
                str(payload.get("outcome") or ""),
            )

            if publisher:
                await publisher.publish_task_completed(
                    agent_id=row["owner_id"] or reviewer_id,
                    task_id=str(task_id),
                    title=row["task"],
                    evidence=str(payload.get("evidence") or payload.get("details") or ""),
                    source=row["source"] or "completion_review",
                )

            status = "approved"
        else:
            await conn.fetchrow(
                """
                UPDATE backlog
                SET
                    status = 'pending',
                    review_status = 'needs_input',
                    reviewer_id = $2,
                    reviewed_at = NOW(),
                    review_notes = $3,
                    retry_count = COALESCE(retry_count, 0) + 1,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                task_id,
                reviewer_id,
                notes,
            )

            if publisher:
                await publisher.publish_task_failed(
                    agent_id=row["owner_id"] or reviewer_id,
                    task_id=str(task_id),
                    title=row["task"],
                    reason=f"completion_review_failed:{reason}",
                    source=row["source"] or "completion_review",
                    evidence=str(payload.get("evidence") or ""),
                )

            status = "rework"

        return status + ":" + reason

    finally:
        await conn.close()
        if publisher and getattr(publisher, "cluster", None):
            await publisher.cluster.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Review task completion outcome")
    parser.add_argument("--db-url", type=str, default=None)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--result-json", type=str, default=None, help="Inline JSON string")
    parser.add_argument("--result-file", type=str, default=None, help="JSON file path")
    parser.add_argument("--publish-nats", action="store_true")
    args = parser.parse_args()

    if args.result_json and args.result_file:
        raise SystemExit("Use only one of --result-json or --result-file")

    payload = {}
    if args.result_file:
        with open(args.result_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    elif args.result_json:
        payload = json.loads(args.result_json)
    else:
        # fallback: allow key=value style from stdin to reduce friction
        raw = input("Enter completion payload JSON: ").strip()
        if raw:
            payload = json.loads(raw)

    db_url = args.db_url or os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL missing")

    import asyncio
    result = asyncio.run(review_completion(db_url, args.task_id, payload, args.reviewer_id, args.publish_nats))
    print(result)


if __name__ == "__main__":
    main()
