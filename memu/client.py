"""Python client for memU API."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from memu.models import (
    BulkImportResponse,
    ChatResponse,
    Memory,
    MemoryCreate,
    MemoryType,
    SearchResult,
)


class MemUClient:
    """Client for interacting with a memU server.

    Usage:
        client = MemUClient("http://localhost:8000", api_key="your-key")
        client.add("The sky is blue", memory_type="fact", agent_id="rosie")
        results = client.search("sky color")
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
        )

    def health(self) -> dict:
        """Check if the server is healthy."""
        r = self._client.get("/health")
        r.raise_for_status()
        return r.json()

    def add(
        self,
        content: str,
        *,
        memory_type: str | MemoryType = "fact",
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        parent_id: str | UUID | None = None,
        confidence: float = 1.0,
    ) -> Memory:
        """Store a memory."""
        payload = MemoryCreate(
            content=content,
            memory_type=MemoryType(memory_type),
            agent_id=agent_id,
            metadata=metadata or {},
            parent_id=UUID(str(parent_id)) if parent_id else None,
            confidence=confidence,
        )
        r = self._client.post("/memories", json=payload.model_dump(mode="json"))
        r.raise_for_status()
        return Memory.model_validate(r.json())

    def get(self, memory_id: str | UUID) -> Memory:
        """Get a specific memory by ID."""
        r = self._client.get(f"/memories/{memory_id}")
        r.raise_for_status()
        return Memory.model_validate(r.json())

    def delete(self, memory_id: str | UUID) -> None:
        """Delete a memory."""
        r = self._client.delete(f"/memories/{memory_id}")
        r.raise_for_status()

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        agent_id: str | None = None,
        memory_type: str | MemoryType | None = None,
        temporal_weight: float = 0.3,
        min_confidence: float = 0.0,
    ) -> list[SearchResult]:
        """Semantic search over memories."""
        payload = {
            "query": query,
            "limit": limit,
            "agent_id": agent_id,
            "memory_type": MemoryType(memory_type).value if memory_type else None,
            "temporal_weight": temporal_weight,
            "min_confidence": min_confidence,
        }
        r = self._client.post("/search", json=payload)
        r.raise_for_status()
        return [SearchResult.model_validate(item) for item in r.json()]

    def chat(
        self,
        question: str,
        *,
        agent_id: str | None = None,
        context_limit: int = 10,
    ) -> ChatResponse:
        """RAG chat — ask questions over your memory base."""
        payload = {
            "question": question,
            "agent_id": agent_id,
            "context_limit": context_limit,
        }
        r = self._client.post("/chat", json=payload)
        r.raise_for_status()
        return ChatResponse.model_validate(r.json())

    def bulk_import(
        self,
        content: str,
        *,
        agent_id: str | None = None,
        memory_type: str | MemoryType = "fact",
        split_on: str = "\n\n",
    ) -> BulkImportResponse:
        """Bulk import memories from text/markdown."""
        payload = {
            "content": content,
            "agent_id": agent_id,
            "memory_type": MemoryType(memory_type).value,
            "split_on": split_on,
        }
        r = self._client.post("/memories/bulk", json=payload)
        r.raise_for_status()
        return BulkImportResponse.model_validate(r.json())

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
