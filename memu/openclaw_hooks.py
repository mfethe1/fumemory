"""OpenClaw Memory Hooks — strict mandate logging for all agent actions.

Every agent action (search, edit, exec, decision) gets logged to memU
with full audit trail: who, what, when, why, and outcome.

Usage from OpenClaw cron/agent:
    python -m memu.openclaw_hooks log-action \\
        --agent lenny \\
        --type web_search \\
        --description "Searched for Notion API version compatibility" \\
        --outcome "Found 2022-06-28 vs 2025-09-03 breaking change" \\
        --task-id "optional-notion-task-id"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx

# Use client if available for proper logging, else fallback to direct API
try:
    from memu.client import MemUClient
    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False

logger = logging.getLogger(__name__)

MEMU_BASE_URL = os.environ.get("MEMU_BASE_URL", "http://localhost:8000")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")


async def log_action(
    agent_id: str,
    action_type: str,
    description: str,
    outcome: str = "success",
    task_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Log an agent action to memU as an audit memory."""
    
    if HAS_CLIENT:
        # Use the new client method for strict audit logging
        client = MemUClient(MEMU_BASE_URL, MEMU_API_KEY)
        try:
            await client.log_action(
                agent_id=agent_id,
                tool=action_type,
                input=description,
                output=outcome,
                session_id=task_id or "unknown"
            )
            return {"status": "logged_via_client"}
        except Exception as e:
            logger.warning(f"Client logging failed, falling back to legacy hook: {e}")

    # Fallback / Legacy Hook logic (stores as memory)
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
        "memory_type": "observation",  # Updated to new schema
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

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            f"{MEMU_BASE_URL}/memories",
            json=payload,
            headers={"X-API-Key": MEMU_API_KEY, "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()

# ... (rest of the file remains similar or simplified for brevity in this overwrite)

def main():
    parser = argparse.ArgumentParser(description="OpenClaw Memory Hooks")
    sub = parser.add_subparsers(dest="command")

    act = sub.add_parser("log-action")
    act.add_argument("--agent", required=True)
    act.add_argument("--type", required=True)
    act.add_argument("--description", required=True)
    act.add_argument("--outcome", default="success")
    act.add_argument("--task-id", default=None)

    args = parser.parse_args()

    if args.command == "log-action":
        result = asyncio.run(log_action(
            agent_id=args.agent,
            action_type=args.type,
            description=args.description,
            outcome=args.outcome,
            task_id=args.task_id,
        ))
        print(json.dumps(result, indent=2, default=str))

if __name__ == "__main__":
    main()
