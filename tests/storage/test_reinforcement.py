"""Reinforcement-on-duplicate tests (upstream port E).

Covers the core behaviors: stable content_hash under whitespace/case,
reinforcement counter on duplicate writes, side-channel-field
preservation, and the explicit ``reinforce_node`` path.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from memu.storage import get_backend
from memu.storage.base import WikiNode


def _node(slug: str, **kw) -> WikiNode:
    return WikiNode(
        id=str(uuid.uuid4()),
        slug=slug,
        kind=kw.pop("kind", "note"),
        title=kw.pop("title", slug),
        body=kw.pop("body", "body text"),
        **kw,
    )


# ---------------------------------------------------------------------------
# content_hash stability
# ---------------------------------------------------------------------------


def test_content_hash_stable_under_whitespace_noise():
    a = _node("n", body="Hello world")
    b = _node("n", body="hello  world\n")
    c = _node("n", body="HELLO\r\nworld\t")
    assert a.content_hash() == b.content_hash() == c.content_hash()


def test_content_hash_differs_when_title_differs():
    a = _node("n", title="T1", body="body")
    b = _node("n", title="T2", body="body")
    assert a.content_hash() != b.content_hash()


def test_content_hash_differs_when_body_differs():
    a = _node("n", body="alpha")
    b = _node("n", body="beta")
    assert a.content_hash() != b.content_hash()


# ---------------------------------------------------------------------------
# SQLite backend reinforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_put_bumps_reinforcement(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        first = await backend.put_node(_node("hello", body="Hello world"))
        assert first.reinforcement_count == 0

        # Equivalent body (whitespace only) — should reinforce, not overwrite.
        second = await backend.put_node(_node("hello", body="hello  WORLD\n"))
        assert second.reinforcement_count == 1
        assert second.last_reinforced_at is not None

        # Another equivalent write.
        third = await backend.put_node(_node("hello", body="HELLO world"))
        assert third.reinforcement_count == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_reinforcement_preserves_side_channel_fields(tmp_path):
    """Same body + new happened_at/extra should *merge* without losing them.

    This guards the regression that the original port caused: content-hash
    equality silently dropped new happened_at / extra values.
    """
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        await backend.put_node(_node("n", body="body"))

        event_time = datetime(2022, 1, 1, tzinfo=timezone.utc)
        enriched = _node(
            "n",
            body="body",  # unchanged
            happened_at=event_time,
            extra={"source": "slack-import", "ts": "2022-01-01"},
            tags=["imported"],
        )
        result = await backend.put_node(enriched)
        assert result.reinforcement_count == 1
        assert result.happened_at == event_time
        assert result.extra == {"source": "slack-import", "ts": "2022-01-01"}
        assert result.tags == ["imported"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_different_body_resets_reinforcement(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        # First write + one reinforcement via an equivalent body.
        await backend.put_node(_node("n", body="original"))
        await backend.put_node(_node("n", body="original"))
        existing = await backend.get_node("n")
        assert existing is not None and existing.reinforcement_count == 1

        # Real edit — callers reusing the slug are expected to reuse the id.
        existing.body = "something new"
        changed = await backend.put_node(existing)
        assert changed.reinforcement_count == 0
        assert changed.last_reinforced_at is None
        assert "new" in changed.body
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_explicit_reinforce_node_bumps_without_touching_body(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        await backend.put_node(_node("n", body="unchanged"))
        result = await backend.reinforce_node("n")
        assert result is not None
        assert result.reinforcement_count == 1
        assert result.body == "unchanged"
        # Missing slugs return None.
        assert await backend.reinforce_node("nonexistent") is None
    finally:
        await backend.close()
