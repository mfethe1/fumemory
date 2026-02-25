from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx


class SessionHookError(RuntimeError):
    pass


@dataclass
class SessionHookConfig:
    base_url: str
    api_key: str
    agent_id: str
    timeout_seconds: float = 15.0
    max_retries: int = 3
    initial_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 5.0


class SessionMemUHook:
    def __init__(self, config: SessionHookConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=self.config.timeout_seconds)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "SessionMemUHook":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def write_decision(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self.write_event(
            namespace="decision",
            content=content,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )

    async def write_memory(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self.write_event(
            namespace="memory_write",
            content=content,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )

    async def write_event(
        self,
        *,
        namespace: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not content.strip():
            raise SessionHookError("content must not be empty")

        idem_key = idempotency_key or str(uuid4())
        payload = {
            "content": content,
            "metadata": {
                "agent": self.config.agent_id,
                "namespace": namespace,
                "source": "session_hook",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "confidence": "high",
                "supersedes": None,
                "expires": None,
                "idempotency_key": idem_key,
                **(metadata or {}),
            },
        }
        response = await self._request_with_retry(
            "POST",
            "/api/v1/memu/add",
            json_payload=payload,
            idempotency_key=idem_key,
        )
        if isinstance(response, dict):
            return response
        return {"ok": True, "results": response}

    async def search_text(
        self,
        query: str,
        *,
        limit: int = 10,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []

        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "agent_id": self.config.agent_id,
        }
        if memory_type:
            payload["memory_type"] = memory_type

        response = await self._request_with_retry(
            "POST",
            "/api/v1/memu/search-text",
            json_payload=payload,
            idempotency_key=None,
        )
        if isinstance(response, list):
            return response
        if isinstance(response, dict) and isinstance(response.get("results"), list):
            return response["results"]
        return []

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> Any:
        base = self.config.base_url.rstrip("/")
        url = f"{base}{path}"

        headers = {
            "Content-Type": "application/json",
            "X-MemU-Key": self.config.api_key,
            "X-API-Key": self.config.api_key,
        }
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key

        last_error: str | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_payload,
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt >= self.config.max_retries:
                    break
                await asyncio.sleep(self._backoff_seconds(attempt))
                continue

            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError:
                    return {"ok": True}

            retryable = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            if retryable and attempt < self.config.max_retries:
                await asyncio.sleep(self._backoff_seconds(attempt))
                continue

            last_error = f"HTTP {response.status_code}: {response.text}"
            break

        raise SessionHookError(
            f"memU request failed after {self.config.max_retries} attempts: {last_error or 'unknown error'}"
        )

    def _backoff_seconds(self, attempt: int) -> float:
        window = self.config.initial_backoff_seconds * (2 ** (attempt - 1))
        return min(self.config.max_backoff_seconds, window)
