"""Fail-silent memU hook helpers for OpenClaw event logging.

This module is the Python reference implementation for durable shared-memory logging.
It is designed to back OpenClaw pre/post tool hooks and task lifecycle hooks without
breaking the agent runtime if memU is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("memu.openclaw_hooks")

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RETRIES = 2
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class HookEvent:
    event_type: str
    agent_id: str
    content: str
    memory_type: str = "observation"
    metadata: dict[str, Any] | None = None


class OpenClawHookLogger:
    """Small fail-silent logger for OpenClaw hook events -> memU."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = (base_url or os.environ.get("MEMU_API_URL") or os.environ.get("MEMU_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("MEMU_API_KEY", "")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "OpenClawHookLogger":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def build_resume_query(
        self,
        *,
        agent_id: str,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        query_bits = [f"agent:{agent_id}"]
        if task_id:
            query_bits.append(f"task:{task_id}")
        if session_id:
            query_bits.append(f"session:{session_id}")
        query_bits.append("checkpoint OR task_complete OR error OR tool_post")
        return {
            "query": " ".join(query_bits),
            "agent_id": agent_id,
            "limit": limit,
            "entity_weight": 0.2,
        }

    async def search_resume_context(
        self,
        *,
        agent_id: str,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        payload = self.build_resume_query(
            agent_id=agent_id,
            session_id=session_id,
            task_id=task_id,
            limit=limit,
        )
        result = await self._request("POST", "/api/v1/memu/search", json=payload)
        return result if isinstance(result, list) else []

    async def log_event(self, event: HookEvent) -> bool:
        if not event.content.strip() or not self.api_key:
            return False

        payload = {
            "content": event.content,
            "agent_id": event.agent_id,
            "memory_type": event.memory_type,
            "metadata": {
                "source": "openclaw_hook",
                "event_type": event.event_type,
                "logged_at": datetime.now(timezone.utc).isoformat(),
                **(event.metadata or {}),
            },
        }

        result = await self._request("POST", "/api/v1/memu/add", json=payload)
        return bool(result and result.get("ok", False))

    async def log_task_start(self, agent_id: str, task_id: str, *, session_id: str | None = None, gateway_id: str | None = None, metadata: dict[str, Any] | None = None) -> bool:
        return await self.log_event(
            HookEvent(
                event_type="task_start",
                agent_id=agent_id,
                memory_type="plan",
                content=f"[task_start] agent={agent_id} task={task_id}",
                metadata={
                    "task_id": task_id,
                    "session_id": session_id,
                    "gateway_id": gateway_id,
                    **(metadata or {}),
                },
            )
        )

    async def log_tool_pre(self, agent_id: str, tool_name: str, *, task_id: str | None = None, session_id: str | None = None, gateway_id: str | None = None, tool_call_id: str | None = None, metadata: dict[str, Any] | None = None) -> bool:
        return await self.log_event(
            HookEvent(
                event_type="tool_pre",
                agent_id=agent_id,
                memory_type="observation",
                content=f"[tool_pre] agent={agent_id} tool={tool_name}",
                metadata={
                    "task_id": task_id,
                    "session_id": session_id,
                    "gateway_id": gateway_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "phase": "pre",
                    **(metadata or {}),
                },
            )
        )

    async def log_tool_post(self, agent_id: str, tool_name: str, *, status: str = "ok", task_id: str | None = None, session_id: str | None = None, gateway_id: str | None = None, tool_call_id: str | None = None, metadata: dict[str, Any] | None = None) -> bool:
        memory_type = "observation" if status == "ok" else "failure"
        return await self.log_event(
            HookEvent(
                event_type="tool_post",
                agent_id=agent_id,
                memory_type=memory_type,
                content=f"[tool_post] agent={agent_id} tool={tool_name} status={status}",
                metadata={
                    "task_id": task_id,
                    "session_id": session_id,
                    "gateway_id": gateway_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "phase": "post",
                    "status": status,
                    **(metadata or {}),
                },
            )
        )

    async def log_task_complete(self, agent_id: str, task_id: str, *, status: str = "ok", session_id: str | None = None, gateway_id: str | None = None, metadata: dict[str, Any] | None = None) -> bool:
        memory_type = "decision" if status == "ok" else "failure"
        return await self.log_event(
            HookEvent(
                event_type="task_complete",
                agent_id=agent_id,
                memory_type=memory_type,
                content=f"[task_complete] agent={agent_id} task={task_id} status={status}",
                metadata={
                    "task_id": task_id,
                    "session_id": session_id,
                    "gateway_id": gateway_id,
                    "status": status,
                    **(metadata or {}),
                },
            )
        )

    async def log_error(self, agent_id: str, *, error: str, task_id: str | None = None, session_id: str | None = None, gateway_id: str | None = None, metadata: dict[str, Any] | None = None) -> bool:
        return await self.log_event(
            HookEvent(
                event_type="error",
                agent_id=agent_id,
                memory_type="failure",
                content=f"[error] agent={agent_id} error={error[:240]}",
                metadata={
                    "task_id": task_id,
                    "session_id": session_id,
                    "gateway_id": gateway_id,
                    "error": error,
                    **(metadata or {}),
                },
            )
        )

    async def _request(self, method: str, path: str, *, json: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]] | None:
        if not self.api_key:
            return None

        headers = {
            "Content-Type": "application/json",
            "X-MemU-Key": self.api_key,
            "X-API-Key": self.api_key,
        }
        url = f"{self.base_url}{path}"
        last_error: str | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.request(method, url, headers=headers, json=json)
                if response.status_code < 400:
                    return response.json()
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 2.0))
                    continue
                break
            except Exception as exc:  # fail-silent by design
                last_error = str(exc)
                if attempt < self.max_retries:
                    await asyncio.sleep(min(0.5 * (2 ** (attempt - 1)), 2.0))
                    continue
                break

        logger.warning("memU hook write skipped: %s", last_error or "unknown error")
        return None


async def log_action(agent_id: str, action: str, details: dict[str, Any]) -> bool:
    async with OpenClawHookLogger() as hook:
        return await hook.log_event(
            HookEvent(
                event_type=action,
                agent_id=agent_id,
                memory_type="user_action",
                content=f"[action] agent={agent_id} action={action}",
                metadata={"details": details},
            )
        )


async def log_search(agent_id: str, query: str, results_summary: str) -> bool:
    async with OpenClawHookLogger() as hook:
        return await hook.log_event(
            HookEvent(
                event_type="web_search",
                agent_id=agent_id,
                memory_type="external",
                content=f"[web_search] agent={agent_id} query={query}",
                metadata={"query": query, "results_summary": results_summary[:1000]},
            )
        )


async def recall(query: str, agent_id: str) -> str | None:
    async with OpenClawHookLogger() as hook:
        results = await hook.search_resume_context(agent_id=agent_id, limit=3)
    if not results:
        return None
    top = results[0]
    memory = top.get("memory", {}) if isinstance(top, dict) else {}
    return memory.get("content") if isinstance(memory, dict) else None
