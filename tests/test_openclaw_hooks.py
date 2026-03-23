import json

import httpx
import pytest

from memu.openclaw_hooks import OpenClawHookLogger


@pytest.mark.asyncio
async def test_log_task_start_posts_to_memu_add_compat_endpoint():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/memu/add"
        assert request.headers["X-MemU-Key"] == "test-key"
        assert request.headers["X-API-Key"] == "test-key"
        body = json.loads((await request.aread()).decode())
        assert body["metadata"]["event_type"] == "task_start"
        assert body["metadata"]["task_id"] == "task-123"
        return httpx.Response(200, json={"ok": True, "id": "mem-1", "message": "stored"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://memu.test") as raw_client:
        hook = OpenClawHookLogger(base_url="https://memu.test", api_key="test-key", client=raw_client)
        ok = await hook.log_task_start("winnie", "task-123", session_id="session-1", gateway_id="gw-1")
        assert ok is True


@pytest.mark.asyncio
async def test_search_resume_context_uses_search_compat_endpoint():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/memu/search"
        body = json.loads((await request.aread()).decode())
        assert body["agent_id"] == "winnie"
        assert "task:task-123" in body["query"]
        return httpx.Response(200, json=[{"memory": {"content": "latest checkpoint"}}])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://memu.test") as raw_client:
        hook = OpenClawHookLogger(base_url="https://memu.test", api_key="test-key", client=raw_client)
        results = await hook.search_resume_context(agent_id="winnie", task_id="task-123")
        assert results[0]["memory"]["content"] == "latest checkpoint"


@pytest.mark.asyncio
async def test_hook_is_fail_silent_when_transport_fails():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://memu.test") as raw_client:
        hook = OpenClawHookLogger(base_url="https://memu.test", api_key="test-key", client=raw_client, max_retries=1)
        ok = await hook.log_error("winnie", error="timeout", task_id="task-123")
        assert ok is False
