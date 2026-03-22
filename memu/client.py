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

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        transport = httpx.HTTPTransport(retries=max_retries)
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
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


    def get_recent(
        self, limit: int = 10, agent_id: str | None = None, memory_type: str | None = None
    ) -> list[Memory]:
        params = {"limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        if memory_type:
            params["memory_type"] = memory_type
        r = self._client.get("/memories/recent", params=params)
        r.raise_for_status()
        return [Memory.model_validate(item) for item in r.json()]

    def get_stats(self) -> dict:
        """Get total memory counts by agent and type."""
        r = self._client.get("/api/v1/memu/stats")
        r.raise_for_status()
        return r.json()

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


    def format_for_prompt(self, memories: list[Memory]) -> str:
        """Format a list of memories into a prompt-ready string."""
        if not memories:
            return ""
        output = ["<retrieved_memories>"]
        for m in memories:
            source = f"agent={m.agent_id}" if m.agent_id else "system"
            output.append(f"  <memory id=\"{m.id}\" source=\"{source}\" type=\"{m.memory_type.value}\">")
            output.append(f"    {m.content.strip()}")
            output.append("  </memory>")
        output.append("</retrieved_memories>")
        return "\n".join(output)
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

    def summarize(
        self,
        topic: str,
        *,
        agent_id: str | None = None,
        context_limit: int = 20,
    ) -> str:
        """Helper to get a concise summary of memories related to a topic."""
        prompt = f"Summarize everything we know about '{topic}'. Be concise and use bullet points. Ignore irrelevant context."
        resp = self.chat(prompt, agent_id=agent_id, context_limit=context_limit)
        return resp.answer

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
