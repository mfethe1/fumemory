"""Notion ↔ memU bridge.

This module coordinates tasks between a Notion Agent Task Board and memU.
Agents can claim tasks, complete tasks, and block tasks while completion notes
are synced back into memU.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx


logger = logging.getLogger(__name__)


class NotionBridge:
    """Coordinate task state between Notion and memU with resilient API calls."""

    notion_token: str
    memu_base_url: str
    memu_api_key: str
    task_board_id: str
    memory_log_id: str
    notion_api_version: str

    _notion_client: httpx.AsyncClient
    _memu_client: httpx.AsyncClient
    _backoff_base_seconds: float = 0.5
    _max_retries: int = 3

    def __init__(
        self,
        notion_token: str,
        memu_base_url: str,
        memu_api_key: str,
        *,
        task_board_id: str | None = None,
        memory_log_id: str | None = None,
        notion_api_version: str = "2022-06-28",
    ) -> None:
        """Create a notion bridge instance.

        Args:
            notion_token: Notion integration token used for all notion calls.
            memu_base_url: Base URL for memU API.
            memu_api_key: API key passed in X-API-Key header to memU.
            task_board_id: Notion database ID for Agent Task Board.
            memory_log_id: Notion database ID for Agent Memory Log.
            notion_api_version: Notion API version.
        """
        self.notion_token = notion_token
        self.memu_base_url = memu_base_url.rstrip("/")
        self.memu_api_key = memu_api_key
        self.task_board_id = task_board_id or os.environ.get("NOTION_TASK_BOARD_ID", "")
        self.memory_log_id = memory_log_id or os.environ.get("NOTION_MEMORY_LOG_ID", "")
        self.notion_api_version = notion_api_version
        self._notion_client = httpx.AsyncClient(
            base_url="https://api.notion.com",
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {notion_token}",
                "Notion-Version": notion_api_version,
                "Content-Type": "application/json",
            },
        )
        self._memu_client = httpx.AsyncClient(
            base_url=self.memu_base_url,
            timeout=30.0,
            headers={"X-API-Key": memu_api_key, "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        """Close underlying HTTP clients."""
        await self._notion_client.aclose()
        await self._memu_client.aclose()

    async def poll_tasks(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """Return unclaimed tasks for a given agent (or Any when `agent_id` is None)."""
        if not self.task_board_id:
            raise RuntimeError("NOTION_TASK_BOARD_ID is required to poll tasks")

        filters: list[dict[str, Any]] = [
            {"property": "Status", "status": {"equals": "Backlog"}},
        ]
        if agent_id:
            normalized_agent_id = self._normalize_agent_name(agent_id)
            filters.append(
                {
                    "or": [
                        {"property": "Assigned Agent", "select": {"equals": "Any"}},
                        {"property": "Assigned Agent", "select": {"equals": normalized_agent_id}},
                    ]
                }
            )

        payload = {
            "filter": {"and": filters},
            "sorts": [
                {"property": "Priority", "direction": "ascending"},
                {"timestamp": "created_time", "direction": "descending"},
            ],
        }

        response = await self._request_with_retry(
            self._notion_client,
            "POST",
            f"/v1/databases/{self.task_board_id}/query",
            json=payload,
        )
        data = response.json()

        tasks: list[dict[str, Any]] = []
        for result in data.get("results", []):
            props = result.get("properties", {})
            tasks.append(
                {
                    "id": result.get("id"),
                    "notion_id": result.get("id"),
                    "title": self._extract_title(props.get("Title", {})),
                    "status": self._extract_select(props.get("Status", {})),
                    "assigned_agent": self._extract_select(props.get("Assigned Agent", {})),
                    "priority": self._extract_select(props.get("Priority", {})),
                    "project": self._extract_rich_text(props.get("Project", {})),
                    "notes": self._extract_rich_text(props.get("Agent Notes", {})),
                    "claimed_at": self._extract_date(props.get("Claimed At", {})),
                    "completed_at": self._extract_date(props.get("Completed At", {})),
                    "task_id": self._extract_rich_text(props.get("Task ID", {})),
                }
            )

        return tasks

    async def claim_task(self, task_id: str, agent_id: str) -> dict[str, Any]:
        """Claim a task for `agent_id` and mark it as In Progress."""
        if not task_id:
            raise ValueError("task_id is required")
        if not agent_id:
            raise ValueError("agent_id is required")

        payload = {
            "properties": {
                "Status": {"select": {"name": "In Progress"}},
                "Assigned Agent": {"select": {"name": self._normalize_agent_name(agent_id)}},
                "Claimed At": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
            }
        }

        response = await self._request_with_retry(
            self._notion_client,
            "PATCH",
            f"/v1/pages/{task_id}",
            json=payload,
        )
        return response.json()

    async def complete_task(
        self,
        task_id: str,
        agent_id: str,
        notes: str,
        memory_type: str = "lesson",
    ) -> dict[str, Any]:
        """Mark a task complete and sync completion notes back into memU.

        If memU returns 401, logs the issue and continues (to avoid blocking task
        lifecycle in notion).
        """
        if not task_id:
            raise ValueError("task_id is required")
        if not notes:
            raise ValueError("notes are required")
        if not agent_id:
            raise ValueError("agent_id is required")

        task = await self._get_task(task_id)
        title = self._extract_title(task.get("properties", {}).get("Title", {}))
        existing_notes = self._extract_rich_text(task.get("properties", {}).get("Agent Notes", {}))

        payload = {
            "properties": {
                "Status": {"select": {"name": "Done"}},
                "Completed At": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
                "Agent Notes": {
                    "rich_text": [
                        {
                            "text": {
                                "content": self._merge_notes(
                                    existing_notes,
                                    notes,
                                )
                            }
                        }
                    ]
                },
            }
        }

        response = await self._request_with_retry(
            self._notion_client,
            "PATCH",
            f"/v1/pages/{task_id}",
            json=payload,
        )

        await self._write_memory_safely(
            content=(f"{title}: {notes}".strip()),
            agent_id=agent_id,
            memory_type=memory_type,
        )
        return response.json()

    async def block_task(self, task_id: str, agent_id: str, reason: str) -> dict[str, Any]:
        """Mark task as blocked and append reason into Agent Notes."""
        if not task_id:
            raise ValueError("task_id is required")
        if not reason:
            raise ValueError("reason is required")

        task = await self._get_task(task_id)
        existing_notes = self._extract_rich_text(task.get("properties", {}).get("Agent Notes", {}))

        payload = {
            "properties": {
                "Status": {"select": {"name": "Blocked"}},
                "Agent Notes": {
                    "rich_text": [
                        {
                            "text": {
                                "content": self._merge_notes(
                                    existing_notes,
                                    f"[{agent_id}] Blocked: {reason}",
                                )
                            }
                        }
                    ]
                },
            }
        }

        response = await self._request_with_retry(
            self._notion_client,
            "PATCH",
            f"/v1/pages/{task_id}",
            json=payload,
        )
        return response.json()

    async def create_task(
        self,
        title: str,
        priority: str = "P2",
        project: str = "",
        assigned_agent: str = "Any",
    ) -> str:
        """Create a new task in the Agent Task Board and return new page id."""
        if not title:
            raise ValueError("title is required")
        if not self.task_board_id:
            raise RuntimeError("NOTION_TASK_BOARD_ID is required to create tasks")

        task_uid = str(uuid.uuid4())
        payload = {
            "parent": {"database_id": self.task_board_id},
            "properties": {
                "Title": {"title": [{"text": {"content": title}}]},
                "Status": {"select": {"name": "Backlog"}},
                "Assigned Agent": {"select": {"name": self._normalize_agent_name(assigned_agent)}},
                "Priority": {"select": {"name": priority}},
                "Project": {"rich_text": [{"text": {"content": project}}]} if project else {"rich_text": []},
                "Agent Notes": {"rich_text": []},
                "Task ID": {"rich_text": [{"text": {"content": task_uid}}]},
            },
        }

        response = await self._request_with_retry(
            self._notion_client,
            "POST",
            "/v1/pages",
            json=payload,
        )
        return response.json()["id"]

    async def health_check(self) -> dict[str, Any]:
        """Check Notion and memU reachability, returning a compact status structure."""
        errors: list[str] = []
        notion_ok = False
        memu_ok = False

        if self.task_board_id:
            try:
                await self._request_with_retry(
                    self._notion_client,
                    "GET",
                    f"/v1/databases/{self.task_board_id}",
                    json=None,
                )
                notion_ok = True
            except Exception as exc:  # pragma: no cover - network dependent
                errors.append(f"notion: {exc}")
        else:
            errors.append("notion: NOTION_TASK_BOARD_ID is not set")

        try:
            response = await self._request_with_retry(
                self._memu_client,
                "GET",
                "/health",
                json=None,
            )
            _ = response.json()
            memu_ok = True
        except Exception as exc:  # pragma: no cover - network dependent
            errors.append(f"memu: {exc}")

        return {"notion_ok": notion_ok, "memu_ok": memu_ok, "errors": errors}

    async def _write_memory_safely(
        self,
        *,
        content: str,
        agent_id: str,
        memory_type: str,
    ) -> None:
        """Write a memory to memU and never raise on 401s."""
        memory_payload = {
            "content": content,
            "agent_id": agent_id,
            "memory_type": self._normalize_memory_type(memory_type),
        }

        try:
            await self._request_with_retry(
                self._memu_client,
                "POST",
                "/memories",
                json=memory_payload,
                allowed_statuses=(200, 201),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                logger.error("memU returned 401 when writing task completion memory; continuing")
                return
            logger.error("Failed writing memory after retries: %s", exc)
            raise
        except Exception as exc:
            logger.error("Failed writing task completion memory: %s", exc)
            raise

    async def _get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch a notion page for a single task id."""
        response = await self._request_with_retry(
            self._notion_client,
            "GET",
            f"/v1/pages/{task_id}",
            json=None,
        )
        return response.json()

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        allowed_statuses: tuple[int, ...] = (200,),
        fail_on_status: Callable[[int], bool] | None = None,
    ) -> httpx.Response:
        """Send request with bounded retries and exponential backoff."""
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await client.request(method, path, json=json)

                if response.status_code not in allowed_statuses:
                    if fail_on_status is not None and fail_on_status(response.status_code):
                        response.raise_for_status()
                    if 400 <= response.status_code < 500:
                        response.raise_for_status()
                    if response.status_code >= 500:
                        response.raise_for_status()

                return response
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    status_code = exc.response.status_code
                    if 400 <= status_code < 500 and status_code not in (429, 500, 502, 503, 504):
                        # Non-retryable client error
                        raise
                else:
                    status_code = None

                logger.warning(
                    "Request failed (attempt %s/%s) to %s %s: %s",
                    attempt,
                    self._max_retries,
                    method,
                    path,
                    exc,
                )
                last_error = exc

                if attempt == self._max_retries:
                    raise last_error

                await asyncio.sleep(self._backoff_base_seconds * (2 ** (attempt - 1)))

            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Unexpected error performing request %s %s", method, path)
                raise exc

        if last_error is not None:
            raise last_error
        raise RuntimeError("Request failed unexpectedly")

    @staticmethod
    def _extract_rich_text(property_value: dict[str, Any]) -> str:
        """Read plain text from a Notion rich_text property."""
        parts = property_value.get("rich_text", [])
        return "".join(part.get("plain_text", "") for part in parts)

    @staticmethod
    def _extract_title(property_value: dict[str, Any]) -> str:
        """Read plain text from a Notion title property."""
        parts = property_value.get("title", [])
        return "".join(part.get("plain_text", "") for part in parts)

    @staticmethod
    def _extract_select(property_value: dict[str, Any]) -> str:
        """Read plain text from a Notion select property."""
        selected = property_value.get("select")
        if selected is None:
            return ""
        return selected.get("name", "")

    @staticmethod
    def _extract_date(property_value: dict[str, Any]) -> str | None:
        """Return date string for a Notion date property."""
        date_obj = property_value.get("date")
        if date_obj is None:
            return None
        return date_obj.get("start")

    @staticmethod
    def _normalize_agent_name(agent_id: str) -> str:
        """Normalize an agent string to Notion select case."""
        clean = agent_id.strip().lower()
        if clean == "any":
            return "Any"
        if clean == "macklemore":
            return "Macklemore"
        return clean.capitalize()

    @staticmethod
    def _normalize_memory_type(memory_type: str) -> str:
        """Normalize and clamp memory_type into one of memU types."""
        normalized = memory_type.strip().lower()
        valid = {"fact", "decision", "lesson", "pattern", "failure"}
        if normalized not in valid:
            logger.warning("Invalid memory_type '%s', defaulting to lesson", memory_type)
            return "lesson"
        return normalized

    @staticmethod
    def _merge_notes(existing_notes: str, notes: str) -> str:
        """Combine preexisting and new notes, avoiding duplicates/noise."""
        note_parts = [note for note in [existing_notes.strip(), notes.strip()] if note]
        return "\n".join(note_parts)


async def create_bridge_from_env() -> NotionBridge:
    """Create a NotionBridge using environment configuration."""
    notion_token = os.environ.get("NOTION_API_KEY", "").strip()
    memu_base_url = os.environ.get("MEMU_BASE_URL", "http://api:8000").rstrip("/")
    memu_api_key = os.environ.get("MEMU_API_KEY", "").strip()
    task_board_id = os.environ.get("NOTION_TASK_BOARD_ID", "").strip()
    memory_log_id = os.environ.get("NOTION_MEMORY_LOG_ID", "").strip()

    if not notion_token:
        raise RuntimeError("NOTION_API_KEY is required")

    return NotionBridge(
        notion_token=notion_token,
        memu_base_url=memu_base_url,
        memu_api_key=memu_api_key,
        task_board_id=task_board_id,
        memory_log_id=memory_log_id,
    )
