"""Hardening utilities for robust memU communication.

Includes:
- /health checks and embedding dimension validation
- Key rotation alerting
- memU API wrapper with retries, auth recovery, and circuit breaker behavior
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class MemUHealthChecker:
    """Helpers for checking memU health and embedding behavior."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        embedding_base_url: str,
        embedding_model: str,
        timeout: float = 20.0,
    ) -> None:
        """Create a health checker with API and embedding configs."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.embedding_base_url = embedding_base_url.rstrip("/")
        self.embedding_model = embedding_model
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        )

    async def check(self) -> dict[str, Any]:
        """Call /health and return a normalized status payload."""
        try:
            response = await self.client.get("/health")
            response.raise_for_status()
            payload = response.json()
            return {
                "ok": True,
                "status": payload.get("status", "unknown"),
                "raw": payload,
            }
        except Exception as exc:
            logger.error("memU /health check failed: %s", exc)
            return {"ok": False, "status": "unhealthy", "error": str(exc)}

    async def validate_embedding_dims(self, expected_dims: int = 384) -> bool:
        """Request a dummy embedding and check returned dimensionality."""
        url = f"{self.embedding_base_url}/v1/embeddings"
        headers = {"Content-Type": "application/json"}
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if openai_api_key:
            headers["Authorization"] = f"Bearer {openai_api_key}"

        try:
            async with httpx.AsyncClient(timeout=20.0, headers=headers) as emb_client:
                r = await emb_client.post(
                    url,
                    json={"input": "dimension check", "model": self.embedding_model},
                )
                r.raise_for_status()
                data = r.json()
                embedding = data["data"][0]["embedding"]
                actual = len(embedding)
                if actual != expected_dims:
                    logger.error("Embedding dimension mismatch: expected=%s actual=%s", expected_dims, actual)
                    return False
                return True
        except Exception as exc:
            logger.error("Embedding dimension validation failed: %s", exc)
            return False

    async def rotate_key_alert(self) -> None:
        """Warn if memU reports key rotation age over 4 days."""
        health = await self.check()
        if not health.get("ok"):
            return

        raw = health.get("raw", {})
        last_rotation = raw.get("last_key_rotation")
        if not last_rotation:
            return

        try:
            parsed = datetime.fromisoformat(str(last_rotation).replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - parsed
            if age > timedelta(days=4):
                logger.warning(
                    "memU API key may be stale: rotated %s days ago",
                    age.days,
                )
        except Exception as exc:
            logger.error("Failed to parse last_key_rotation='%s': %s", last_rotation, exc)


class MemUClient:
    """Resilient async client for memU with auth/retry and breaker protections."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        embedding_base_url: str,
        embedding_model: str,
        expected_dims: int = 384,
    ) -> None:
        """Construct a memU client wrapper.

        Args:
            base_url: memU base URL.
            api_key: API key for X-API-Key header.
            embedding_base_url: embedding endpoint host.
            embedding_model: model used for embedding generation.
            expected_dims: expected embedding dimensionality.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.expected_dims = expected_dims
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        )
        self.embedding_base_url = embedding_base_url.rstrip("/")
        self.embedding_model = embedding_model

        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0

    async def _embedding_dims(self, text: str) -> int | None:
        """Compute vector dimensions for text using embedding endpoint."""
        headers = {"Content-Type": "application/json"}
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        if openai_api_key:
            headers["Authorization"] = f"Bearer {openai_api_key}"

        url = f"{self.embedding_base_url}/v1/embeddings"
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as emb_client:
            resp = await emb_client.post(
                url,
                json={"input": text, "model": self.embedding_model},
            )
            resp.raise_for_status()
            payload = resp.json()
            embedding = payload["data"][0]["embedding"]
            return len(embedding)

    def _circuit_open(self) -> bool:
        """Return whether circuit breaker is actively blocking calls."""
        return self._circuit_open_until > time.time()

    def _record_success(self) -> None:
        """Reset failure counters after successful request."""
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        """Record failure and open breaker after threshold reached."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= 5:
            self._circuit_open_until = time.time() + (5 * 60)
            logger.error("memU circuit breaker opened for 5 minutes after repeated failures")

    async def request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> httpx.Response:
        """Perform request with 401 retries and circuit breaker protection."""
        if self._circuit_open():
            delay = self._circuit_open_until - time.time()
            raise RuntimeError(f"memU circuit breaker active for another {delay:.1f}s")

        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                response = await self.client.request(method, path, json=json)
                response.raise_for_status()
                self._record_success()
                return response
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    logger.warning("memU auth failed on attempt %s/%s for %s", attempt, attempts, path)
                    self._record_failure()
                elif 500 <= exc.response.status_code <= 599:
                    logger.warning("memU transient server error %s on attempt %s/%s", exc.response.status_code, attempt, attempts)
                    self._record_failure()
                else:
                    self._record_failure()
                    raise
                if attempt == attempts:
                    raise
                await asyncio.sleep(0.4 * (2 ** (attempt - 1)))
            except httpx.RequestError as exc:
                logger.warning("memU network error on attempt %s/%s: %s", attempt, attempts, exc)
                self._record_failure()
                if attempt == attempts:
                    raise
                await asyncio.sleep(0.4 * (2 ** (attempt - 1)))

        raise RuntimeError("memU request failed")

    async def create_memory(
        self,
        content: str,
        agent_id: str,
        memory_type: str = "lesson",
    ) -> dict[str, Any]:
        """Create memory while validating content embedding dimensions."""
        dims = await self._embedding_dims(content)
        if dims is not None and dims != self.expected_dims:
            logger.error(
                "Embedding dimension mismatch on write: expected=%s actual=%s",
                self.expected_dims,
                dims,
            )

        payload = {
            "content": content,
            "agent_id": agent_id,
            "memory_type": memory_type,
        }
        response = await self.request("POST", "/memories", json=payload)
        return response.json()

    async def close(self) -> None:
        """Close underlying transport."""
        await self.client.aclose()
