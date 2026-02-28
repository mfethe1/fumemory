"""OpenClaw Memory Hooks — strict mandate logging for all agent actions.

Every agent action (search, edit, exec, decision) gets logged to memU
with full audit trail: who, what, when, why, and outcome.

Usage from OpenClaw cron/agent:
    python -m memu.openclaw_hooks log-action \
        --agent lenny \
        --type web_search \
        --description "Searched for Notion API version compatibility" \
        --outcome "Found 2022-06-28 vs 2025-09-03 breaking change" \
        --task-id "optional-notion-task-id"

    python -m memu.openclaw_hooks log-search \
        --agent lenny \
        --query "NATS JetStream python tutorial" \
        --source brave \
        --results '["url1", "url2"]'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

MEMU_BASE_URL = os.environ.get("MEMU_BASE_URL", "http://localhost:8000")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
MEMU_PROD_URL = os.environ.get("MEMU_PROD_URL", "https://api-production-86f5.up.railway.app")


async def log_action(
    agent_id: str,
    action_type: str,
    description: str,
    outcome: str = "success",
    task_id: str | None = None,
    metadata: dict | None = None,
    use_prod: bool = False,
) -> dict:
    """Log an agent action to memU as an audit memory."""
    base_url = MEMU_PROD_URL if use_prod else MEMU_BASE_URL
    
    content = (
        f"[ACTION] {action_type} by {agent_id}\n"
        f"Description: {description}\n"
        f"Outcome: {outcome}\n"
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}"
    )
    if task_id:
        content += f"\nTask: {task_id}"

    payload = {
        "content": content,
        "memory_type": "fact",
        "agent_id": agent_id,
        "metadata": {
            "action_type": action_type,
            "outcome": outcome,
            "task_id": task_id,
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "hook": "openclaw_action_log",
            **(metadata or {}),
        },
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{base_url}/memories",
            json=payload,
            headers={"X-API-Key": MEMU_API_KEY, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()


async def log_search(
    agent_id: str,
    query: str,
    source: str = "brave",
    results: list[str] | None = None,
    summary: str | None = None,
    use_prod: bool = False,
) -> dict:
    """Log a web search to memU for the search cache."""
    base_url = MEMU_PROD_URL if use_prod else MEMU_BASE_URL

    content = (
        f"[SEARCH] {source} by {agent_id}\n"
        f"Query: {query}\n"
        f"Results: {len(results or [])}\n"
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}"
    )
    if summary:
        content += f"\nSummary: {summary}"
    if results:
        content += f"\nURLs: {', '.join(results[:5])}"

    payload = {
        "content": content,
        "memory_type": "fact",
        "agent_id": agent_id,
        "metadata": {
            "action_type": "web_search",
            "search_query": query,
            "search_source": source,
            "result_count": len(results or []),
            "result_urls": (results or [])[:10],
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "hook": "openclaw_search_log",
        },
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{base_url}/memories",
            json=payload,
            headers={"X-API-Key": MEMU_API_KEY, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()


async def recall_searches(
    query: str,
    agent_id: str | None = None,
    limit: int = 5,
    use_prod: bool = False,
) -> list[dict]:
    """Check if we've searched for something similar before."""
    base_url = MEMU_PROD_URL if use_prod else MEMU_BASE_URL

    payload = {
        "query": f"SEARCH {query}",
        "limit": limit,
    }
    if agent_id:
        payload["agent_id"] = agent_id

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{base_url}/search",
            json=payload,
            headers={"X-API-Key": MEMU_API_KEY, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        results = r.json()
        # Filter to only search-type memories
        return [
            r for r in results
            if r.get("memory", {}).get("metadata", {}).get("hook") == "openclaw_search_log"
        ]


def main():
    parser = argparse.ArgumentParser(description="OpenClaw Memory Hooks")
    sub = parser.add_subparsers(dest="command")

    # log-action
    act = sub.add_parser("log-action")
    act.add_argument("--agent", required=True)
    act.add_argument("--type", required=True, dest="action_type")
    act.add_argument("--description", required=True)
    act.add_argument("--outcome", default="success")
    act.add_argument("--task-id", default=None)
    act.add_argument("--prod", action="store_true")

    # log-search
    srch = sub.add_parser("log-search")
    srch.add_argument("--agent", required=True)
    srch.add_argument("--query", required=True)
    srch.add_argument("--source", default="brave")
    srch.add_argument("--results", default=None, help="JSON array of URLs")
    srch.add_argument("--summary", default=None)
    srch.add_argument("--prod", action="store_true")

    # recall
    rec = sub.add_parser("recall")
    rec.add_argument("--query", required=True)
    rec.add_argument("--agent", default=None)
    rec.add_argument("--limit", type=int, default=5)
    rec.add_argument("--prod", action="store_true")

    args = parser.parse_args()

    if args.command == "log-action":
        result = asyncio.run(log_action(
            agent_id=args.agent,
            action_type=args.action_type,
            description=args.description,
            outcome=args.outcome,
            task_id=args.task_id,
            use_prod=args.prod,
        ))
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "log-search":
        urls = json.loads(args.results) if args.results else None
        result = asyncio.run(log_search(
            agent_id=args.agent,
            query=args.query,
            source=args.source,
            results=urls,
            summary=args.summary,
            use_prod=args.prod,
        ))
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "recall":
        results = asyncio.run(recall_searches(
            query=args.query,
            agent_id=args.agent,
            limit=args.limit,
            use_prod=args.prod,
        ))
        print(f"Found {len(results)} prior searches:")
        for r in results:
            mem = r.get("memory", {})
            meta = mem.get("metadata", {})
            print(f"  [{meta.get('logged_at', '?')[:10]}] {meta.get('search_query', '?')}")
            print(f"    Source: {meta.get('search_source', '?')}, Results: {meta.get('result_count', '?')}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
