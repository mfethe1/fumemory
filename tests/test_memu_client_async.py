import json

import httpx
import pytest

from memu.client import AsyncMemUClient


@pytest.mark.asyncio
async def test_async_client_ping_true_when_health_ok():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "test-key"
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://memu.test",
        headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
    ) as raw_client:
        client = AsyncMemUClient("https://memu.test", "test-key")
        await client._client.aclose()
        client._client = raw_client
        assert await client.ping() is True


@pytest.mark.asyncio
async def test_async_client_add_posts_memory_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/memories"
        body = json.loads((await request.aread()).decode())
        assert body["agent_id"] == "winnie"
        assert body["memory_type"] == "observation"
        return httpx.Response(
            200,
            json={
                "id": "11111111-1111-1111-1111-111111111111",
                "content": "hello",
                "memory_type": "observation",
                "agent_id": "winnie",
                "metadata": {},
                "parent_id": None,
                "confidence": 1.0,
                "access_count": 0,
                "decay_score": 1.0,
                "created_at": "2026-03-23T17:00:00Z",
                "updated_at": "2026-03-23T17:00:00Z",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://memu.test",
        headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
    ) as raw_client:
        client = AsyncMemUClient("https://memu.test", "test-key")
        await client._client.aclose()
        client._client = raw_client
        memory = await client.add("hello", memory_type="observation", agent_id="winnie")
        assert memory.agent_id == "winnie"
        assert memory.content == "hello"


@pytest.mark.asyncio
async def test_async_client_search_uses_expected_contract():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        body = json.loads((await request.aread()).decode())
        assert body["query"] == "resume context"
        assert body["agent_id"] == "winnie"
        return httpx.Response(
            200,
            json=[
                {
                    "memory": {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "content": "Recovered checkpoint",
                        "memory_type": "plan",
                        "agent_id": "winnie",
                        "metadata": {"event_type": "checkpoint"},
                        "parent_id": None,
                        "confidence": 1.0,
                        "access_count": 0,
                        "decay_score": 1.0,
                        "created_at": "2026-03-23T17:00:00Z",
                        "updated_at": "2026-03-23T17:00:00Z",
                    },
                    "similarity": 0.91,
                    "final_score": 0.93,
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://memu.test",
        headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
    ) as raw_client:
        client = AsyncMemUClient("https://memu.test", "test-key")
        await client._client.aclose()
        client._client = raw_client
        results = await client.search("resume context", agent_id="winnie")
        assert len(results) == 1
        assert results[0].memory.content == "Recovered checkpoint"
