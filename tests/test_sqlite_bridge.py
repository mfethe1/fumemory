"""Tests for tools/sqlite_bridge.py.

Covers:
- Column detection (_pick_column)
- Memory type mapping (map_memory_type)
- Row-to-payload conversion (row_to_payload)
- SQLite reading with in-file databases (read_rows_after)
- State persistence (load_state / save_state)
- memu POST with mocked HTTP (post_memory)
- Full file sync with deduplication (sync_file)
- Multi-file sweep (sync_all)

All tests run without a live memu instance or real OpenClaw SQLite files.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from tools.sqlite_bridge import (
    _CONTENT_CANDIDATES,
    _pick_column,
    load_state,
    map_memory_type,
    post_memory,
    read_rows_after,
    row_to_payload,
    save_state,
    sync_all,
    sync_file,
)

_MEMU = "http://memu-test:8000"
_KEY = "test-api-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(path: Path, rows: list[dict]) -> None:
    """Create a SQLite database with a 'memories' table populated with rows."""
    conn = sqlite3.connect(str(path))
    if rows:
        cols = list(rows[0].keys())
        conn.execute(f"CREATE TABLE memories ({', '.join(cols)})")
        for row in rows:
            placeholders = ", ".join(["?" for _ in cols])
            conn.execute(
                f"INSERT INTO memories ({', '.join(cols)}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
    conn.commit()
    conn.close()


def _mem_resp(n: int = 1) -> dict:
    return {
        "id": f"00000000-0000-0000-0000-{n:012d}",
        "content": f"row {n}",
        "memory_type": "fact",
        "memory_kind": "evidence",
        "agent_id": "mack",
        "metadata": {},
        "parent_id": None,
        "confidence": 1.0,
        "access_count": 0,
        "salience_score": 0.5,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# _pick_column
# ---------------------------------------------------------------------------


def test_pick_column_finds_first_match():
    assert _pick_column(_CONTENT_CANDIDATES, ["id", "value", "created_at"]) == "value"


def test_pick_column_case_insensitive():
    assert _pick_column(_CONTENT_CANDIDATES, ["id", "Value", "ts"]) == "Value"


def test_pick_column_prefers_earlier_candidate():
    # "value" appears before "content" in _CONTENT_CANDIDATES
    assert _pick_column(_CONTENT_CANDIDATES, ["content", "value"]) == "value"


def test_pick_column_returns_none_when_absent():
    assert _pick_column(_CONTENT_CANDIDATES, ["id", "rowid", "xyz"]) is None


# ---------------------------------------------------------------------------
# map_memory_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("fact", "fact"),
        ("Lesson", "lesson"),
        ("DECISION", "decision"),
        ("pattern", "pattern"),
        ("failure", "failure"),
        ("note", "observation"),
        ("knowledge", "lesson"),
        ("unknown_type", "fact"),
        (None, "fact"),
        ("", "fact"),
    ],
)
def test_map_memory_type(raw, expected):
    assert map_memory_type(raw) == expected


# ---------------------------------------------------------------------------
# row_to_payload
# ---------------------------------------------------------------------------


def test_row_to_payload_basic_columns():
    row = {"value": "Paris is the capital of France", "type": "fact", "created_at": "2024-01-01"}
    payload = row_to_payload(row, "mack", 42)

    assert payload["content"] == "Paris is the capital of France"
    assert payload["memory_type"] == "fact"
    assert payload["memory_kind"] == "evidence"
    assert payload["agent_id"] == "mack"
    assert payload["idempotency_key"] == "sqlite:mack:42"
    assert payload["metadata"]["sqlite_source"] == "mack"
    assert payload["metadata"]["sqlite_rowid"] == 42
    assert payload["metadata"]["source_timestamp"] == "2024-01-01"
    assert payload["metadata"]["bridge"] == "sqlite_agent_bridge"


def test_row_to_payload_fallback_content_when_no_known_column():
    row = {"key": "theme", "thing": "dark mode"}
    payload = row_to_payload(row, "lenny", 7)

    assert "dark mode" in payload["content"]
    assert payload["agent_id"] == "lenny"


def test_row_to_payload_no_type_defaults_to_fact():
    row = {"content": "Some observation"}
    payload = row_to_payload(row, "mack", 1)
    assert payload["memory_type"] == "fact"


def test_row_to_payload_idempotency_key_is_stable():
    row = {"content": "x"}
    k1 = row_to_payload(row, "mack", 1)["idempotency_key"]
    k2 = row_to_payload(row, "mack", 1)["idempotency_key"]
    assert k1 == k2 == "sqlite:mack:1"


def test_row_to_payload_different_agents_get_different_keys():
    row = {"content": "shared fact"}
    k_mack = row_to_payload(row, "mack", 1)["idempotency_key"]
    k_lenny = row_to_payload(row, "lenny", 1)["idempotency_key"]
    assert k_mack != k_lenny


def test_row_to_payload_different_rowids_get_different_keys():
    row = {"content": "same text"}
    k1 = row_to_payload(row, "mack", 1)["idempotency_key"]
    k2 = row_to_payload(row, "mack", 2)["idempotency_key"]
    assert k1 != k2


# ---------------------------------------------------------------------------
# read_rows_after — real SQLite, in-file database
# ---------------------------------------------------------------------------


def test_read_rows_after_returns_all_rows_from_zero(tmp_path):
    db = tmp_path / "mack.sqlite"
    _make_db(db, [
        {"content": "First fact", "type": "fact"},
        {"content": "Second fact", "type": "lesson"},
    ])

    rows, new_max = read_rows_after(db, 0)
    assert len(rows) == 2
    assert new_max == 2


def test_read_rows_after_respects_last_rowid(tmp_path):
    db = tmp_path / "mack.sqlite"
    _make_db(db, [
        {"content": "Old row", "type": "fact"},
        {"content": "New row", "type": "lesson"},
    ])

    rows, new_max = read_rows_after(db, 1)
    assert len(rows) == 1
    row_dict, rowid = rows[0]
    assert row_dict["content"] == "New row"
    assert rowid == 2
    assert new_max == 2


def test_read_rows_after_returns_empty_when_no_new_rows(tmp_path):
    db = tmp_path / "mack.sqlite"
    _make_db(db, [{"content": "Old", "type": "fact"}])

    rows, new_max = read_rows_after(db, 1)
    assert rows == []
    assert new_max == 1


def test_read_rows_after_empty_table(tmp_path):
    db = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE memories (content TEXT)")
    conn.commit()
    conn.close()

    rows, new_max = read_rows_after(db, 0)
    assert rows == []
    assert new_max == 0


def test_read_rows_after_no_user_tables(tmp_path):
    db = tmp_path / "notables.sqlite"
    conn = sqlite3.connect(str(db))
    conn.commit()
    conn.close()

    rows, new_max = read_rows_after(db, 0)
    assert rows == []
    assert new_max == 0


def test_read_rows_after_rowids_are_integers(tmp_path):
    db = tmp_path / "mack.sqlite"
    _make_db(db, [{"content": "fact"}])

    rows, _ = read_rows_after(db, 0)
    _, rid = rows[0]
    assert isinstance(rid, int)


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------


def test_state_roundtrip(tmp_path):
    sf = tmp_path / "state.json"
    state = {"file_a.sqlite": 10, "file_b.sqlite": 42}
    save_state(sf, state)
    loaded = load_state(sf)
    assert loaded == state


def test_load_state_returns_empty_dict_when_file_missing(tmp_path):
    sf = tmp_path / "nonexistent.json"
    assert load_state(sf) == {}


def test_load_state_returns_empty_dict_on_corrupt_json(tmp_path):
    sf = tmp_path / "bad.json"
    sf.write_text("not valid json{{{")
    assert load_state(sf) == {}


def test_save_state_creates_parent_dirs(tmp_path):
    sf = tmp_path / "nested" / "deep" / "state.json"
    save_state(sf, {"x": 1})
    assert sf.exists()
    assert json.loads(sf.read_text()) == {"x": 1}


# ---------------------------------------------------------------------------
# post_memory — mocked memu API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_memory_success_returns_response():
    payload = {
        "content": "Paris is the capital of France",
        "memory_type": "fact",
        "memory_kind": "evidence",
        "agent_id": "mack",
        "idempotency_key": "sqlite:mack:1",
    }
    with respx.mock:
        respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(200, json=_mem_resp(1))
        )
        async with httpx.AsyncClient() as client:
            result = await post_memory(client, payload, _MEMU, _KEY)

    assert result is not None
    assert "id" in result


@pytest.mark.asyncio
async def test_post_memory_201_also_succeeds():
    with respx.mock:
        respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(201, json=_mem_resp(1))
        )
        async with httpx.AsyncClient() as client:
            result = await post_memory(client, {"content": "x"}, _MEMU, _KEY)

    assert result is not None


@pytest.mark.asyncio
async def test_post_memory_409_returns_none():
    with respx.mock:
        respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(409, json={"error": "idempotency_conflict"})
        )
        async with httpx.AsyncClient() as client:
            result = await post_memory(
                client, {"content": "x", "idempotency_key": "sqlite:mack:99"}, _MEMU, _KEY
            )
    assert result is None


@pytest.mark.asyncio
async def test_post_memory_500_returns_none():
    with respx.mock:
        respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        async with httpx.AsyncClient() as client:
            result = await post_memory(client, {"content": "x"}, _MEMU, _KEY)
    assert result is None


@pytest.mark.asyncio
async def test_post_memory_network_error_returns_none():
    with respx.mock:
        respx.post(f"{_MEMU}/memories").mock(side_effect=httpx.ConnectError("down"))
        async with httpx.AsyncClient() as client:
            result = await post_memory(client, {"content": "x"}, _MEMU, _KEY)
    assert result is None


@pytest.mark.asyncio
async def test_post_memory_sends_api_key_header():
    with respx.mock:
        route = respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(200, json=_mem_resp())
        )
        async with httpx.AsyncClient() as client:
            await post_memory(client, {"content": "x"}, _MEMU, "my-secret-key")

    assert route.calls.last.request.headers["X-MemU-Key"] == "my-secret-key"


# ---------------------------------------------------------------------------
# sync_file — integration: read SQLite + POST to memu
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_file_imports_all_rows(tmp_path):
    db = tmp_path / "mack.sqlite"
    _make_db(db, [
        {"content": "row 1", "type": "fact"},
        {"content": "row 2", "type": "lesson"},
    ])
    state: dict[str, int] = {}

    with respx.mock:
        route = respx.post(f"{_MEMU}/memories").mock(
            side_effect=[
                httpx.Response(200, json=_mem_resp(1)),
                httpx.Response(200, json=_mem_resp(2)),
            ]
        )
        async with httpx.AsyncClient() as client:
            count = await sync_file(db, state, client, _MEMU, _KEY)

    assert count == 2
    assert state[str(db)] == 2
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_sync_file_no_repost_on_second_run(tmp_path):
    """State tracking prevents re-importing already-seen rows."""
    db = tmp_path / "mack.sqlite"
    _make_db(db, [{"content": "fact one", "type": "fact"}])
    state: dict[str, int] = {}

    with respx.mock:
        route = respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(200, json=_mem_resp(1))
        )
        async with httpx.AsyncClient() as client:
            await sync_file(db, state, client, _MEMU, _KEY)
            count2 = await sync_file(db, state, client, _MEMU, _KEY)

    assert count2 == 0
    assert route.call_count == 1  # only one POST total


@pytest.mark.asyncio
async def test_sync_file_only_imports_new_rows_after_append(tmp_path):
    db = tmp_path / "mack.sqlite"
    _make_db(db, [{"content": "existing row", "type": "fact"}])
    state: dict[str, int] = {}

    with respx.mock:
        respx.post(f"{_MEMU}/memories").mock(return_value=httpx.Response(200, json=_mem_resp(1)))
        async with httpx.AsyncClient() as client:
            await sync_file(db, state, client, _MEMU, _KEY)

    # Append a new row to the database
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO memories (content, type) VALUES (?, ?)", ("new row", "lesson"))
    conn.commit()
    conn.close()

    with respx.mock:
        route2 = respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(200, json=_mem_resp(2))
        )
        async with httpx.AsyncClient() as client:
            count = await sync_file(db, state, client, _MEMU, _KEY)

    assert count == 1
    assert route2.call_count == 1
    assert state[str(db)] == 2


@pytest.mark.asyncio
async def test_sync_file_uses_agent_name_from_filename(tmp_path):
    db = tmp_path / "lenny.sqlite"
    _make_db(db, [{"content": "lenny knows this"}])
    state: dict[str, int] = {}

    with respx.mock:
        route = respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(200, json=_mem_resp(1))
        )
        async with httpx.AsyncClient() as client:
            await sync_file(db, state, client, _MEMU, _KEY)

    body = json.loads(route.calls.last.request.content)
    assert body["agent_id"] == "lenny"
    assert body["idempotency_key"].startswith("sqlite:lenny:")


@pytest.mark.asyncio
async def test_sync_file_skips_missing_db_gracefully(tmp_path):
    db = tmp_path / "missing.sqlite"
    state: dict[str, int] = {}

    with respx.mock:
        async with httpx.AsyncClient() as client:
            count = await sync_file(db, state, client, _MEMU, _KEY)

    assert count == 0
    assert str(db) not in state


@pytest.mark.asyncio
async def test_sync_file_sends_evidence_kind(tmp_path):
    db = tmp_path / "mack.sqlite"
    _make_db(db, [{"content": "some fact"}])

    with respx.mock:
        route = respx.post(f"{_MEMU}/memories").mock(
            return_value=httpx.Response(200, json=_mem_resp(1))
        )
        async with httpx.AsyncClient() as client:
            await sync_file(db, {}, client, _MEMU, _KEY)

    body = json.loads(route.calls.last.request.content)
    assert body["memory_kind"] == "evidence"


# ---------------------------------------------------------------------------
# sync_all — multi-file sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_all_processes_multiple_agents(tmp_path):
    for agent in ("mack", "lenny"):
        _make_db(tmp_path / f"{agent}.sqlite", [{"content": f"{agent} memory", "type": "fact"}])

    state_file = tmp_path / "state.json"

    with respx.mock:
        respx.post(f"{_MEMU}/memories").mock(
            side_effect=[
                httpx.Response(200, json=_mem_resp(1)),
                httpx.Response(200, json=_mem_resp(1)),
            ]
        )
        async with httpx.AsyncClient() as client:
            total = await sync_all(tmp_path, {}, state_file, client, _MEMU, _KEY)

    assert total == 2
    assert state_file.exists()


@pytest.mark.asyncio
async def test_sync_all_saves_state_after_sweep(tmp_path):
    db = tmp_path / "mack.sqlite"
    _make_db(db, [{"content": "fact"}])
    state_file = tmp_path / "state.json"

    with respx.mock:
        respx.post(f"{_MEMU}/memories").mock(return_value=httpx.Response(200, json=_mem_resp(1)))
        async with httpx.AsyncClient() as client:
            await sync_all(tmp_path, {}, state_file, client, _MEMU, _KEY)

    saved = json.loads(state_file.read_text())
    assert str(db) in saved
    assert saved[str(db)] == 1


@pytest.mark.asyncio
async def test_sync_all_empty_directory(tmp_path):
    state_file = tmp_path / "state.json"
    with respx.mock:
        async with httpx.AsyncClient() as client:
            total = await sync_all(tmp_path, {}, state_file, client, _MEMU, _KEY)
    assert total == 0


@pytest.mark.asyncio
async def test_sync_all_excludes_hidden_sqlite_files(tmp_path):
    """Files starting with '.' (like .memu_bridge_state.json) are excluded."""
    _make_db(tmp_path / ".hidden.sqlite", [{"content": "should not import"}])
    state_file = tmp_path / "state.json"

    with respx.mock(assert_all_called=False):
        async with httpx.AsyncClient() as client:
            total = await sync_all(tmp_path, {}, state_file, client, _MEMU, _KEY)

    assert total == 0
