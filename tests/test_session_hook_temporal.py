from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from memu.session_hook import SessionHookConfig, SessionMemUHook, SessionHookError
from memu.temporal_workflow import MemUHealthAndRepairWorkflow


@pytest.mark.asyncio
async def test_session_hook_write_and_search_text_roundtrip_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/memu/add"):
            payload = json.loads(request.content.decode())
            assert payload["metadata"]["agent"] == "macklemore"
            return httpx.Response(200, json={"ok": True, "id": 42})
        if request.url.path.endswith("/api/v1/memu/search-text"):
            return httpx.Response(200, json=[{"id": 42, "content": "done"}])
        return httpx.Response(404, json={"ok": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        hook = SessionMemUHook(
            SessionHookConfig(
                base_url="https://memu.example",
                api_key="key",
                agent_id="macklemore",
                max_retries=1,
            ),
            client=client,
        )
        write_result = await hook.write_decision("Ship it")
        search_result = await hook.search_text("Ship", limit=2)

    assert write_result["ok"] is True
    assert search_result[0]["id"] == 42


@pytest.mark.asyncio
async def test_session_hook_retry_429_then_recover_with_deterministic_injector(deterministic_429_injector):
    transport, injector = deterministic_429_injector(
        [
            (429, {"ok": False, "error": "rate-limited"}),
            (200, {"ok": True, "id": 101}),
        ]
    )

    async with httpx.AsyncClient(transport=transport, base_url="https://memu.example") as client:
        hook = SessionMemUHook(
            SessionHookConfig(
                base_url="https://memu.example",
                api_key="key",
                agent_id="macklemore",
                max_retries=3,
            ),
            client=client,
        )
        result = await hook.write_memory("Recovered after 429", idempotency_key="idempotency-429")

    assert result["ok"] is True
    assert injector.request_count == 2


@pytest.mark.asyncio
async def test_session_hook_falls_back_to_degraded_error_after_429_exhausted(deterministic_429_injector):
    transport, injector = deterministic_429_injector(
        [
            (429, {"ok": False, "error": "still rate-limited"}),
            (429, {"ok": False, "error": "still rate-limited"}),
            (429, {"ok": False, "error": "still rate-limited"}),
        ]
    )

    async with httpx.AsyncClient(transport=transport) as client:
        hook = SessionMemUHook(
            SessionHookConfig(
                base_url="https://memu.example",
                api_key="key",
                agent_id="macklemore",
                max_retries=3,
            ),
            client=client,
        )
        with pytest.raises(SessionHookError):
            await hook.write_memory("degraded path", idempotency_key="idempotency-429-degraded")

    assert injector.request_count == 3


@pytest.mark.asyncio
async def test_temporal_workflow_persists_and_recovers(tmp_path: Path):
    attempts = {"health": 0, "repair": 0}

    async def health_check(_: dict[str, object]):
        attempts["health"] += 1
        if attempts["health"] == 1:
            return {"ok": False, "reason": "down"}
        return {"ok": True}

    async def repair(_: dict[str, object]):
        attempts["repair"] += 1
        return {"repaired": True}

    workflow = MemUHealthAndRepairWorkflow(
        run_id="workflow-1",
        health_check=health_check,
        repair=repair,
        state_dir=str(tmp_path),
        max_attempts=3,
        retry_delays_seconds=[0],
    )
    state = await workflow.run()

    state_file = tmp_path / "workflow-1.json"
    persisted = json.loads(state_file.read_text())

    assert state.status == "recovered"
    assert persisted["status"] == "recovered"
    assert attempts["repair"] == 1
    assert len(persisted["events"]) >= 4
