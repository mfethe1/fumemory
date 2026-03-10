from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4
import sys
import types

import pytest
from fastapi import HTTPException


class _DummyTemporalClient:
    @staticmethod
    async def connect(*_args, **_kwargs):
        return _DummyTemporalClient()


temporalio = types.ModuleType("temporalio")
temporalio_client = types.ModuleType("temporalio.client")
temporalio_workflow = types.ModuleType("temporalio.workflow")
temporalio_client.Client = _DummyTemporalClient
temporalio_workflow.defn = lambda cls: cls
temporalio_workflow.run = lambda fn: fn
async def _noop_execute_activity(*_args, **_kwargs):
    return None

temporalio_workflow.execute_activity = _noop_execute_activity
temporalio.workflow = temporalio_workflow
sys.modules.setdefault("temporalio", temporalio)
sys.modules.setdefault("temporalio.client", temporalio_client)
sys.modules.setdefault("temporalio.workflow", temporalio_workflow)

from memu import api
from memu.models import MemoryCreate, MemoryType, TaskStatus, TaskUpdate


class FakeConn:
    def __init__(self, existing_row=None, update_row=None, task_row=None):
        self.existing_row = existing_row
        self.update_row = update_row
        self.task_row = task_row
        self.fetchrow_calls = []

    async def fetchrow(self, query, *params):
        self.fetchrow_calls.append((query, params))
        if "FROM memories WHERE custom_id = $1" in query:
            return self.existing_row
        if "UPDATE memories SET" in query:
            return self.update_row
        if "UPDATE backlog" in query:
            return self.task_row
        raise AssertionError(f"Unexpected query: {query}")


@pytest.mark.asyncio
async def test_create_memory_prefers_custom_id_dedupe(monkeypatch):
    memory_id = uuid4()
    now = datetime.now(timezone.utc)
    conn = FakeConn(
        existing_row={"id": memory_id, "similarity": 1.0},
        update_row={
            "id": memory_id,
            "content": "Always run migrations before deploying",
            "memory_type": MemoryType.lesson.value,
            "agent_id": "rosie",
            "metadata": {"project": "memu"},
            "parent_id": None,
            "confidence": 1.0,
            "access_count": 4,
            "decay_score": 1.0,
            "tags": ["deploy", "ops"],
            "custom_id": "deploy-checklist:migrations",
            "duplicate_count": 2,
            "created_at": now,
            "updated_at": now,
        },
    )

    @asynccontextmanager
    async def fake_tenant_conn(_auth):
        yield conn

    async def fake_embedding(_text):
        return None

    monkeypatch.setattr(api, "_tenant_conn", fake_tenant_conn)
    monkeypatch.setattr(api, "get_embedding", fake_embedding)

    req = MemoryCreate(
        content="Always run migrations before deploying",
        memory_type=MemoryType.lesson,
        agent_id="rosie",
        custom_id="deploy-checklist:migrations",
        metadata={"project": "memu"},
        tags=["ops", "deploy"],
    )
    auth = api.AuthContext("memu-dev-key", api.DEFAULT_TENANT_ID)

    memory = await api.create_memory(req, _key=auth)

    assert memory.id == memory_id
    assert memory.custom_id == "deploy-checklist:migrations"
    assert memory.duplicate_count == 2
    assert any("FROM memories WHERE custom_id = $1" in query for query, _ in conn.fetchrow_calls)
    assert any("duplicate_count = COALESCE(duplicate_count, 0) + 1" in query for query, _ in conn.fetchrow_calls)


@pytest.mark.parametrize(
    "content",
    [
        "Authorization: Bearer sk-live-token",
        ": 1710000000:0;cd /tmp\n: 1710000001:0;ls -la\n: 1710000002:0;git status",
    ],
)
def test_memory_hygiene_blocks_secret_and_history_payloads(content):
    req = MemoryCreate(content=content, memory_type=MemoryType.observation, agent_id="rosie")
    with pytest.raises(HTTPException) as exc:
        api._enforce_memory_hygiene(req)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_task_update_requires_evidence_for_handoff(monkeypatch):
    @asynccontextmanager
    async def fake_tenant_conn(_auth):
        yield FakeConn()

    monkeypatch.setattr(api, "_tenant_conn", fake_tenant_conn)

    auth = api.AuthContext("memu-dev-key", api.DEFAULT_TENANT_ID)
    with pytest.raises(HTTPException) as exc:
        await api.update_task(
            uuid4(),
            TaskUpdate(status=TaskStatus.done, metadata={"handoff_to": "lenny"}),
            _key=auth,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_task_update_accepts_evidence_for_handoff(monkeypatch):
    task_id = uuid4()
    now = datetime.now(timezone.utc)
    conn = FakeConn(
        task_row={
            "id": task_id,
            "task": "Ship hygiene patch",
            "priority": "P1",
            "status": TaskStatus.done.value,
            "owner_id": "rosie",
            "lane": "memu",
            "metadata": {"handoff_to": "lenny"},
            "evidence": "pytest tests/test_dedupe_hygiene.py -q",
            "dependency_id": None,
            "created_at": now,
            "updated_at": now,
        }
    )

    @asynccontextmanager
    async def fake_tenant_conn(_auth):
        yield conn

    monkeypatch.setattr(api, "_tenant_conn", fake_tenant_conn)

    auth = api.AuthContext("memu-dev-key", api.DEFAULT_TENANT_ID)
    task = await api.update_task(
        task_id,
        TaskUpdate(
            status=TaskStatus.done,
            evidence="pytest tests/test_dedupe_hygiene.py -q",
            metadata={"handoff_to": "lenny"},
        ),
        _key=auth,
    )

    assert task.id == task_id
    assert task.evidence.startswith("pytest")
