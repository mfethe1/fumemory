from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memu.models import MemoryCreate, SearchRequest
from memu.temporal_client import search_memory_workflow, store_memory_workflow
from memu.temporal_contracts import OrchestrationContext, TemporalAcceptedResponse, TemporalSettings


@dataclass
class DummyHandle:
    id: str


class DummyClient:
    def __init__(self):
        self.start_calls: list[dict] = []
        self.execute_calls: list[dict] = []

    async def start_workflow(self, workflow, *, args, id, task_queue):
        self.start_calls.append(
            {
                "workflow": workflow,
                "args": args,
                "id": id,
                "task_queue": task_queue,
            }
        )
        return DummyHandle(id=id)

    async def execute_workflow(self, workflow, *, args, id, task_queue):
        self.execute_calls.append(
            {
                "workflow": workflow,
                "args": args,
                "id": id,
                "task_queue": task_queue,
            }
        )
        return [
            {
                "memory": {
                    "id": uuid4(),
                    "content": "checkpoint restored",
                    "memory_type": "observation",
                    "agent_id": "winnie",
                    "metadata": {},
                    "parent_id": None,
                    "confidence": 1.0,
                    "access_count": 1,
                    "decay_score": None,
                    "created_at": "2026-03-10T00:00:00Z",
                    "updated_at": "2026-03-10T00:00:00Z",
                },
                "similarity": 0.9,
                "final_score": 0.95,
            }
        ]


@pytest.fixture()
def fake_temporal_workflows_module(monkeypatch):
    module = types.ModuleType("memu.temporal_worker.workflows")

    class MemoryIngestionWorkflow:
        async def run(self, request):  # pragma: no cover - never executed
            return request

    class MemorySearchWorkflow:
        async def run(self, request):  # pragma: no cover - never executed
            return request

    module.MemoryIngestionWorkflow = MemoryIngestionWorkflow
    module.MemorySearchWorkflow = MemorySearchWorkflow
    monkeypatch.setitem(sys.modules, "memu.temporal_worker.workflows", module)
    return module


@pytest.mark.asyncio
async def test_store_memory_workflow_uses_stable_id_and_cross_gateway_payload(monkeypatch, fake_temporal_workflows_module):
    dummy_client = DummyClient()
    monkeypatch.setattr("memu.temporal_client.get_client", AsyncMock(return_value=dummy_client))
    monkeypatch.setattr(
        "memu.temporal_client.load_temporal_settings",
        lambda: TemporalSettings(
            host="temporal:7233",
            namespace="swarm-prod",
            task_queue="gateway-handoff",
            worker_identity="worker:winnie:1",
        ),
    )

    req = MemoryCreate(
        content="preserve checkpoint before handoff",
        agent_id="winnie",
        metadata={"source_gateway": "mack"},
        orchestration=OrchestrationContext(
            task_id=uuid4(),
            source_gateway="mack",
            lease_key="telegram:chat:topic",
        ),
    )

    accepted = await store_memory_workflow(req)

    assert isinstance(accepted, TemporalAcceptedResponse)
    assert accepted.task_queue == "gateway-handoff"
    assert accepted.namespace == "swarm-prod"
    assert len(dummy_client.start_calls) == 1
    start_call = dummy_client.start_calls[0]
    assert start_call["id"] == accepted.workflow_id
    assert start_call["task_queue"] == "gateway-handoff"
    assert start_call["args"][0]["orchestration"]["lease_key"] == "telegram:chat:topic"
    assert start_call["args"][0]["orchestration"]["source_gateway"] == "mack"


@pytest.mark.asyncio
async def test_search_memory_workflow_preserves_search_contract_fields(monkeypatch, fake_temporal_workflows_module):
    dummy_client = DummyClient()
    monkeypatch.setattr("memu.temporal_client.get_client", AsyncMock(return_value=dummy_client))
    monkeypatch.setattr(
        "memu.temporal_client.load_temporal_settings",
        lambda: TemporalSettings(
            host="temporal:7233",
            namespace="default",
            task_queue="memu-queue",
            worker_identity="worker:mack:2",
        ),
    )

    req = SearchRequest(
        query="checkpoint",
        limit=7,
        agent_id="winnie",
        min_confidence=0.4,
        temporal_weight=0.6,
        orchestration=OrchestrationContext(
            task_id=uuid4(),
            source_gateway="lenny",
            lease_key="discord:shared:lane",
        ),
    )

    results = await search_memory_workflow(req)

    assert results is not None
    assert len(dummy_client.execute_calls) == 1
    execute_call = dummy_client.execute_calls[0]
    payload = execute_call["args"][0]
    assert execute_call["task_queue"] == "memu-queue"
    assert payload["query"] == "checkpoint"
    assert payload["limit"] == 7
    assert payload["min_confidence"] == 0.4
    assert payload["temporal_weight"] == 0.6
    assert payload["orchestration"]["source_gateway"] == "lenny"
    assert payload["orchestration"]["lease_key"] == "discord:shared:lane"
