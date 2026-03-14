from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from memu import api
from memu.models import MemoryCreate, MemoryType


@pytest.mark.asyncio
async def test_create_memory_supersedes_marks_previous_versions_and_links(monkeypatch):
    old_id = uuid4()
    inserted_id = uuid4()
    now = datetime.now(timezone.utc)

    inserted_row = {
        "id": inserted_id,
        "content": "Alice moved to Berlin",
        "memory_type": MemoryType.observation.value,
        "agent_id": "lenny",
        "metadata": {},
        "parent_id": None,
        "confidence": 1.0,
        "access_count": 0,
        "decay_score": 1.0,
        "created_at": now,
        "updated_at": now,
    }

    conn = AsyncMock()
    conn.fetchrow.side_effect = [None, inserted_row]
    monkeypatch.setattr(api, "get_embedding", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_nats_publisher", None)

    @asynccontextmanager
    async def fake_tenant_conn(_auth):
        yield conn

    monkeypatch.setattr(api, "_tenant_conn", fake_tenant_conn)

    await api.create_memory(
        MemoryCreate(
            content="Alice moved to Berlin",
            memory_type=MemoryType.observation,
            agent_id="lenny",
            supersedes=old_id,
        ),
        _key=api.AuthContext("memu-dev-key"),
    )

    insert_call = conn.fetchrow.await_args_list[1]
    inserted_metadata = json.loads(insert_call.args[5])
    assert inserted_metadata["supersedes"] == str(old_id)
    assert inserted_metadata["invalidates"] == [str(old_id)]

    execute_calls = conn.execute.await_args_list
    update_call = next(call for call in execute_calls if "UPDATE memories" in call.args[0])
    link_call = next(call for call in execute_calls if "INSERT INTO memory_links" in call.args[0])

    assert update_call.args[1] == inserted_id
    assert update_call.args[3] == [old_id]
    assert link_call.args[1] == inserted_id
    assert link_call.args[2] == [old_id]


def test_filter_current_search_rows_drops_superseded_invalidated_and_stale_memories():
    now = datetime.now(timezone.utc)
    superseded_id = uuid4()
    replacement_id = uuid4()
    stale_id = uuid4()
    valid_id = uuid4()
    invalidated_id = uuid4()

    rows = [
        {
            "id": superseded_id,
            "metadata": {},
            "valid_to": None,
        },
        {
            "id": replacement_id,
            "metadata": {"supersedes": str(superseded_id)},
            "valid_to": None,
        },
        {
            "id": stale_id,
            "metadata": {"stale_after": (now - timedelta(minutes=1)).isoformat()},
            "valid_to": None,
        },
        {
            "id": invalidated_id,
            "metadata": {"invalidated_by": str(replacement_id), "invalidated_at": now.isoformat()},
            "valid_to": None,
        },
        {
            "id": valid_id,
            "metadata": {},
            "valid_to": None,
        },
    ]

    filtered = api._filter_current_search_rows(rows, now=now)
    filtered_ids = {row["id"] for row in filtered}

    assert filtered_ids == {replacement_id, valid_id}


def test_extract_superseding_targets_supports_explicit_and_metadata_lists():
    old_a = uuid4()
    old_b = uuid4()
    old_c = uuid4()

    targets = api._extract_superseding_targets(
        {
            "supersedes": str(old_a),
            "invalidates": [str(old_b), str(old_a)],
            "invalidated_ids": [str(old_c)],
        },
        supersedes=old_a,
        invalidates=[old_b],
    )

    assert targets == [old_a, old_b, old_c]
