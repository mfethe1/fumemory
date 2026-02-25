#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memu.backlog_projection import parse_backlog_markdown
from memu.hardening import sanitize_nats_subject


DEFAULT_BACKLOG = Path(__file__).resolve().parents[2] / "BACKLOG.md"
DEFAULT_SUBJECT_TEMPLATE = "state.task.{entity_id}"


def _event_id_for(task_id: str, status: str, next_action: str | None, blocker: str | None) -> str:
    material = f"{task_id}|{status}|{next_action or ''}|{blocker or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material))


def build_backlog_synced_events(*, backlog_text: str, source_file: str = "BACKLOG.md") -> list[dict]:
    tasks = parse_backlog_markdown(backlog_text, source_file=source_file)
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    events: list[dict] = []
    for i, task in enumerate(tasks, start=1):
        events.append(
            {
                "event_id": _event_id_for(task.task_id, task.status, task.next_action, task.blocker),
                "gateway_id": "backlog-sync",
                "agent_id": "agent/main/macklemore",
                "event_type": "backlog.task.synced",
                "entity_id": task.task_id,
                "ts": ts,
                "version": i,
                "payload": {
                    "task_id": task.task_id,
                    "title": task.title,
                    "owner": task.owner,
                    "lane": task.lane,
                    "status": task.status,
                    "next_action": task.next_action,
                    "blocker": task.blocker,
                    "source_file": task.source_file,
                    "metadata": task.metadata or {},
                },
            }
        )
    return events


async def _publish_events(events: list[dict], *, nats_url: str, subject_template: str) -> int:
    import nats

    nc = await nats.connect(nats_url)
    try:
        js = nc.jetstream()
        for event in events:
            entity_id = sanitize_nats_subject(event["entity_id"])
            subject = subject_template.format(entity_id=entity_id)
            await js.publish(subject, json.dumps(event, sort_keys=True).encode())
        return len(events)
    finally:
        await nc.drain()


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        for row in rows:
            fp.write(json.dumps(row, sort_keys=True))
            fp.write("\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mirror BACKLOG.md tasks into backlog.task.synced STATE_EVENTS")
    p.add_argument("--backlog", type=Path, default=DEFAULT_BACKLOG)
    p.add_argument("--source-file", default="BACKLOG.md")
    p.add_argument("--output-jsonl", type=Path, default=Path("artifacts/state-events/backlog-sync.jsonl"))
    p.add_argument("--nats-url", default="nats://localhost:4222")
    p.add_argument("--subject-template", default=DEFAULT_SUBJECT_TEMPLATE)
    p.add_argument("--publish", action="store_true", help="Publish events to JetStream")
    p.add_argument("--dry-run", action="store_true", help="Do not publish, only emit evidence")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    backlog_path: Path = args.backlog
    if not backlog_path.exists():
        raise SystemExit(f"backlog file not found: {backlog_path}")

    events = build_backlog_synced_events(
        backlog_text=backlog_path.read_text(encoding="utf-8"),
        source_file=args.source_file,
    )
    _append_jsonl(args.output_jsonl, events)

    published = 0
    if args.publish and not args.dry_run:
        published = asyncio.run(
            _publish_events(events, nats_url=args.nats_url, subject_template=args.subject_template)
        )

    print(
        json.dumps(
            {
                "ok": True,
                "backlog": str(backlog_path),
                "events": len(events),
                "published": published,
                "dry_run": bool(args.dry_run or not args.publish),
                "evidence": str(args.output_jsonl),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
