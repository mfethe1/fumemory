"""One-shot script to create Notion databases and persist their IDs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx


WORKSPACE_ID = os.environ.get(
    "NOTION_WORKSPACE_ID",
    "b32df4e3-9102-814a-b487-000367ee087d",
)
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
MEMU_BASE_URL = os.environ.get("MEMU_BASE_URL", "http://localhost:8000").rstrip("/")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
ENV_PATH = Path(".env")


AGENT_TASK_BOARD_SCHEMA: dict[str, Any] = {
    "properties": {
        "Title": {"title": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "Backlog", "color": "default"},
                    {"name": "Claimed", "color": "blue"},
                    {"name": "In Progress", "color": "yellow"},
                    {"name": "Done", "color": "green"},
                    {"name": "Blocked", "color": "red"},
                    {"name": "Needs Review", "color": "orange"},
                ]
            }
        },
        "Assigned Agent": {
            "select": {
                "options": [
                    {"name": "Rosie", "color": "blue"},
                    {"name": "Lenny", "color": "yellow"},
                    {"name": "Macklemore", "color": "purple"},
                    {"name": "Winnie", "color": "orange"},
                    {"name": "Any", "color": "default"},
                ]
            }
        },
        "Priority": {
            "select": {
                "options": [
                    {"name": "P0", "color": "red"},
                    {"name": "P1", "color": "orange"},
                    {"name": "P2", "color": "yellow"},
                    {"name": "P3", "color": "blue"},
                ]
            }
        },
        "Project": {"rich_text": {}},
        "Agent Notes": {"rich_text": {}},
        "Claimed At": {"date": {}},
        "Completed At": {"date": {}},
        "Task ID": {"rich_text": {}},
    }
}

MEMORY_LOG_SCHEMA: dict[str, Any] = {
    "properties": {
        "Title": {"title": {}},
        "Agent": {
            "select": {
                "options": [
                    {"name": "Rosie", "color": "blue"},
                    {"name": "Lenny", "color": "yellow"},
                    {"name": "Macklemore", "color": "purple"},
                    {"name": "Winnie", "color": "orange"},
                ]
            }
        },
        "Project": {"rich_text": {}},
        "Memory Type": {
            "select": {
                "options": [
                    {"name": "fact", "color": "default"},
                    {"name": "decision", "color": "blue"},
                    {"name": "lesson", "color": "yellow"},
                    {"name": "pattern", "color": "green"},
                    {"name": "failure", "color": "red"},
                ]
            }
        },
        "Stored In memU": {"checkbox": {}},
        "Created At": {"date": {}},
    }
}


async def _create_database(client: httpx.AsyncClient, name: str, schema: dict[str, Any]) -> str:
    """Create a Notion database and return its id."""
    payload = {
        "parent": {"type": "workspace", "workspace": True},
        "title": [
            {
                "type": "text",
                "text": {"content": name},
            }
        ],
        "properties": schema["properties"],
    }

    response = await client.post("https://api.notion.com/v1/databases", json=payload)
    response.raise_for_status()
    return response.json()["id"]


def _update_env(ids: dict[str, str]) -> None:
    """Persist notion ids to local .env file."""
    env_lines = []
    if ENV_PATH.exists():
        env_lines = ENV_PATH.read_text().splitlines()

    existing = {line.split("=", 1)[0]: i for i, line in enumerate(env_lines) if "=" in line and not line.strip().startswith("#")}

    for key, value in ids.items():
        line = f"{key}={value}"
        if key in existing:
            env_lines[existing[key]] = line
        else:
            env_lines.append(line)

    ENV_PATH.write_text("\n".join(env_lines) + "\n")


def _check_memu_connectivity() -> bool:
    """Check memU health endpoint and return True when healthy."""
    try:
        headers = {"X-API-Key": MEMU_API_KEY}
        with httpx.Client(timeout=10.0) as client:
            response = client.get(f"{MEMU_BASE_URL}/health", headers=headers)
            response.raise_for_status()
        return True
    except Exception as exc:
        print(f"memU connectivity check failed: {exc}")
        return False


async def main() -> None:
    """Create databases and save IDs to .env."""
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0, headers=headers) as notion_client:
        task_board_id = await _create_database(
            notion_client,
            "Agent Task Board",
            AGENT_TASK_BOARD_SCHEMA,
        )
        memory_log_id = await _create_database(
            notion_client,
            "Agent Memory Log",
            MEMORY_LOG_SCHEMA,
        )

    print(f"Agent Task Board ID: {task_board_id}")
    print(f"Agent Memory Log ID: {memory_log_id}")

    _update_env(
        {
            "NOTION_TASK_BOARD_ID": task_board_id,
            "NOTION_MEMORY_LOG_ID": memory_log_id,
            "NOTION_WORKSPACE_ID": WORKSPACE_ID,
        }
    )
    print(f"Wrote IDs to {ENV_PATH.resolve()}")

    if _check_memu_connectivity():
        print("memU connectivity: OK")
    else:
        print("memU connectivity: FAILED")


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except Exception as exc:
        raise SystemExit(f"Setup failed: {exc}")
