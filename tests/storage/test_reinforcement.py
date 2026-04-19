"""Reinforcement-on-duplicate tests (upstream port E).

Ports upstream memU PR #206 and mirrors the test-style of port D
(`test_happened_at_and_extra.py`): one section per concern (WikiNode
dataclass, SQLite backend, markdown backend, scoring primitive).

Covers stable content_hash under whitespace/case, reinforcement counter
on duplicate writes, side-channel-field preservation, explicit
``reinforce_node``, markdown frontmatter round-trip, the idempotent
SQLite migration, and the ``score_combined`` primitive.
"""
from __future__ import annotations

import math
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest

from memu.rlm.scoring import (
    DEFAULT_RECENCY_HALFLIFE_DAYS,
    days_since,
    recency_decay,
    reinforcement_boost,
    score_combined,
)
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


@pytest.mark.asyncio
async def test_sqlite_migration_upgrades_legacy_schema_with_reinforcement(tmp_path):
    """Legacy SQLite files without reinforcement columns must keep working.

    Creates a schema from the previous port (D) generation — has
    ``extra_json`` + ``happened_at`` but no reinforcement columns — and
    points a fresh backend at it. The idempotent migration should add
    the new columns and the ``nodes_reinforce_idx`` index without
    destroying the existing row.
    """
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            extra_json TEXT NOT NULL DEFAULT '{}',
            source_json TEXT,
            agent_id TEXT NOT NULL DEFAULT 'user',
            memory_type TEXT NOT NULL DEFAULT 'observation',
            salience REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            happened_at TEXT
        );
        CREATE TABLE slug_registry (slug TEXT PRIMARY KEY, node_id TEXT NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE links (src_id TEXT, dst_slug TEXT, type TEXT, strength REAL, PRIMARY KEY(src_id, dst_slug, type));
        """
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO nodes(id, slug, kind, title, body, created_at, updated_at) "
        "VALUES('old-1','legacy','note','Legacy','body',?,?)",
        (now_iso, now_iso),
    )
    conn.execute(
        "INSERT INTO slug_registry(slug, node_id, kind) VALUES('legacy','old-1','note')"
    )
    conn.commit()
    conn.close()

    backend = get_backend(f"sqlite:///{db}")
    await backend.init()
    try:
        got = await backend.get_node("legacy")
        assert got is not None
        assert got.reinforcement_count == 0
        assert got.last_reinforced_at is None

        # Index was created.
        idx = backend.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='nodes_reinforce_idx'"
        ).fetchone()
        assert idx is not None, "nodes_reinforce_idx missing after migration"

        # Full round-trip through the migrated columns.
        bumped = await backend.reinforce_node("legacy")
        assert bumped is not None
        assert bumped.reinforcement_count == 1
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# Markdown backend reinforcement + frontmatter round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_markdown_reinforcement_on_duplicate(tmp_path):
    backend = get_backend(f"file://{tmp_path}")
    await backend.init()
    try:
        first = await backend.put_node(_node("dup", body="same body"))
        assert first.reinforcement_count == 0

        # Second put with equivalent content (trailing newline added + case)
        # should bump, not overwrite. ``id`` on the incoming node is
        # different to prove the backend keys on slug+content, not id.
        again = await backend.put_node(_node("dup", body="SAME body\n"))
        assert again.reinforcement_count == 1
        assert again.last_reinforced_at is not None
        assert again.id == first.id

        # Third equivalent put bumps again.
        third = await backend.put_node(_node("dup", body="  same body  "))
        assert third.reinforcement_count == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_markdown_different_body_resets_reinforcement(tmp_path):
    backend = get_backend(f"file://{tmp_path}")
    await backend.init()
    try:
        await backend.put_node(_node("dup", body="v1"))
        bumped = await backend.put_node(_node("dup", body="v1"))
        assert bumped.reinforcement_count == 1

        replaced = await backend.put_node(_node("dup", body="v2 totally different"))
        assert replaced.reinforcement_count == 0
        assert replaced.last_reinforced_at is None
        assert replaced.body == "v2 totally different"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_markdown_explicit_reinforce_preserves_body(tmp_path):
    backend = get_backend(f"file://{tmp_path}")
    await backend.init()
    try:
        await backend.put_node(_node("dup", body="keep this"))
        r1 = await backend.reinforce_node("dup")
        assert r1 is not None
        # ``dump_frontmatter`` normalizes body to end with a newline;
        # compare after ``.rstrip`` so we assert semantic preservation
        # without coupling to that formatting detail.
        assert r1.body.rstrip() == "keep this"
        assert r1.reinforcement_count == 1

        r2 = await backend.reinforce_node("dup")
        assert r2.reinforcement_count == 2
        assert r2.body.rstrip() == "keep this"

        # Missing ref -> None, not an exception.
        assert await backend.reinforce_node("no-such-slug") is None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_markdown_frontmatter_roundtrips_reinforcement(tmp_path):
    """Reinforcement state must survive a process restart via frontmatter."""
    backend = get_backend(f"file://{tmp_path}")
    await backend.init()
    try:
        await backend.put_node(_node("rr", body="x"))
        await backend.reinforce_node("rr")
        await backend.reinforce_node("rr")
    finally:
        await backend.close()

    # Re-open a fresh backend against the same directory; state must
    # load purely from the markdown frontmatter on disk.
    again = get_backend(f"file://{tmp_path}")
    await again.init()
    try:
        loaded = await again.get_node("rr")
        assert loaded is not None
        assert loaded.reinforcement_count == 2
        assert loaded.last_reinforced_at is not None
    finally:
        await again.close()


def test_frontmatter_omits_reinforcement_when_zero():
    """Unreinforced nodes must not pollute frontmatter output."""
    bare = _node("n1")
    fm = bare.to_frontmatter()
    assert "reinforcement_count" not in fm
    assert "last_reinforced_at" not in fm


def test_frontmatter_emits_reinforcement_when_set():
    ts = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    node = _node("n2", reinforcement_count=3, last_reinforced_at=ts)
    fm = node.to_frontmatter()
    assert fm["reinforcement_count"] == 3
    assert fm["last_reinforced_at"] == ts.isoformat()


# ---------------------------------------------------------------------------
# memu.rlm.scoring — combined score primitive
# ---------------------------------------------------------------------------


def test_score_combined_reasonable_default():
    """Smoke: the example from the backlog spec produces a sensible value."""
    s = score_combined(similarity=0.8, reinforcement=10, recency_days=30)
    assert s > 0
    assert math.isfinite(s)
    # similarity (0.8) × log(10 + 1 + e) (~2.55) × 0.5 (=half-life point)
    # ~= 1.02. Comfortably in range; guards against gross regressions.
    assert 0.9 < s < 1.2


def test_score_combined_monotonic_in_reinforcement():
    """Strictly increasing: un-reinforced floor < reinforcement=1 < ..."""
    def s(r):
        return score_combined(0.5, r, recency_days=0)
    values = [s(r) for r in [0, 1, 2, 5, 20, 100]]
    assert values == sorted(values)
    # All strictly distinct (no plateau).
    assert len(set(values)) == len(values)
    # Un-reinforced nodes still have a non-zero score so they remain
    # ranked against each other by similarity alone.
    assert values[0] > 0
    assert values[1] > values[0]


def test_score_combined_monotonic_decreasing_in_recency_days():
    def s(d):
        return score_combined(0.5, 5, recency_days=d)
    values = [s(d) for d in [0, 1, 7, 30, 90, 365]]
    assert values == sorted(values, reverse=True)
    # Negative "future" recency is clamped to 0, not amplified.
    assert s(-100) == s(0)


def test_recency_decay_halflife_point_is_exactly_half():
    assert recency_decay(DEFAULT_RECENCY_HALFLIFE_DAYS) == pytest.approx(0.5)
    assert recency_decay(0) == 1.0
    assert recency_decay(DEFAULT_RECENCY_HALFLIFE_DAYS * 2) == pytest.approx(0.25)


def test_reinforcement_boost_monotone_and_zero_at_zero():
    assert reinforcement_boost(0) == 0.0
    assert reinforcement_boost(-5) == 0.0
    assert reinforcement_boost(1) == pytest.approx(math.log(2))
    assert reinforcement_boost(10) > reinforcement_boost(1)


def test_days_since_handles_none_and_naive():
    # None -> +inf so downstream decay -> 0 without special casing.
    assert math.isinf(days_since(None))

    now = datetime(2026, 4, 19, tzinfo=timezone.utc)
    past = datetime(2026, 4, 9, tzinfo=timezone.utc)
    assert days_since(past, now=now) == pytest.approx(10.0)

    # Naive datetime is assumed UTC, not raised.
    naive = datetime(2026, 4, 9)
    assert days_since(naive, now=now) == pytest.approx(10.0)
