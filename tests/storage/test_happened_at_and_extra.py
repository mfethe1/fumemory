"""Round-trip tests for the new ``happened_at`` + ``extra`` WikiNode fields.

Ports upstream PR #262 (``happened_at``) and the associated ``extra`` JSON
escape hatch. These fields matter most for importing backlogs where event
time (commit date, meeting time) differs from ingest time — decay curves
and recency-weighted search should use ``happened_at`` when present.
"""
from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from memu.storage import get_backend
from memu.storage.base import WikiNode


def _node(slug: str, **kw) -> WikiNode:
    return WikiNode(
        id=str(uuid.uuid4()),
        slug=slug,
        kind=kw.pop("kind", "note"),
        title=kw.pop("title", slug),
        body=kw.pop("body", "body"),
        **kw,
    )


# ---------------------------------------------------------------------------
# WikiNode dataclass
# ---------------------------------------------------------------------------


def test_effective_time_prefers_happened_at():
    happened = datetime(2022, 1, 1, tzinfo=timezone.utc)
    created = datetime(2026, 4, 1, tzinfo=timezone.utc)
    node = _node("n1", happened_at=happened, created_at=created)
    assert node.effective_time() == happened


def test_effective_time_falls_back_to_created():
    created = datetime(2026, 4, 1, tzinfo=timezone.utc)
    node = _node("n2", created_at=created)
    assert node.effective_time() == created


def test_frontmatter_includes_happened_at_and_extra():
    happened = datetime(2022, 7, 4, tzinfo=timezone.utc)
    node = _node(
        "n3",
        happened_at=happened,
        extra={"pr_number": 262, "author": "upstream@example.com"},
    )
    fm = node.to_frontmatter()
    assert fm["happened_at"] == happened.isoformat()
    assert fm["extra"] == {"pr_number": 262, "author": "upstream@example.com"}


def test_empty_extra_is_omitted_from_frontmatter():
    node = _node("n4")
    assert "extra" not in node.to_frontmatter()


# ---------------------------------------------------------------------------
# Markdown backend roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_markdown_roundtrip_happened_at_and_extra(tmp_path):
    backend = get_backend(f"file://{tmp_path}")
    await backend.init()
    try:
        happened = datetime(2021, 3, 14, tzinfo=timezone.utc)
        node = _node(
            "upstream-port",
            happened_at=happened,
            extra={"ref": "neva/206", "commit": "abc123"},
        )
        await backend.put_node(node)
        roundtripped = await backend.get_node("upstream-port")
        assert roundtripped is not None
        assert roundtripped.happened_at == happened
        assert roundtripped.extra == {"ref": "neva/206", "commit": "abc123"}
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# SQLite backend roundtrip + migration from older schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_roundtrip_happened_at_and_extra(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        happened = datetime(2020, 12, 1, 9, 30, tzinfo=timezone.utc)
        node = _node(
            "commit-abc",
            kind="task",
            happened_at=happened,
            extra={"commit_sha": "abc", "author": "alice@example.com"},
        )
        await backend.put_node(node)
        got = await backend.get_node("commit-abc")
        assert got is not None
        assert got.happened_at == happened
        assert got.extra == {"commit_sha": "abc", "author": "alice@example.com"}
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_migration_upgrades_older_schema_in_place(tmp_path):
    """Vaults created before happened_at/extra_json landed must keep working.

    Simulate an older SQLite file by creating one with a pre-upgrade
    schema, then point a fresh :class:`SqliteBackend` at it. ``init``
    should ``ALTER TABLE`` additively and reads should succeed.
    """
    db = tmp_path / "legacy.db"
    # Hand-build a pre-happened_at schema with just enough shape for a row.
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
            source_json TEXT,
            agent_id TEXT NOT NULL DEFAULT 'user',
            memory_type TEXT NOT NULL DEFAULT 'observation',
            salience REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE slug_registry (slug TEXT PRIMARY KEY, node_id TEXT NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE links (src_id TEXT, dst_slug TEXT, type TEXT, strength REAL, PRIMARY KEY(src_id, dst_slug, type));
        """
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO nodes(id, slug, kind, title, body, created_at, updated_at)
           VALUES('old-1','legacy','note','Legacy','body',?,?)""",
        (now_iso, now_iso),
    )
    conn.execute(
        "INSERT INTO slug_registry(slug, node_id, kind) VALUES('legacy','old-1','note')"
    )
    conn.commit()
    conn.close()

    backend = get_backend(f"sqlite:///{db}")
    await backend.init()  # runs _apply_idempotent_migrations
    try:
        got = await backend.get_node("legacy")
        assert got is not None
        # Columns were added with safe defaults; reading doesn't blow up.
        assert got.happened_at is None
        assert got.extra == {}

        # Writing with the new fields populates the newly-added columns.
        got.happened_at = datetime(2019, 5, 5, tzinfo=timezone.utc)
        got.extra = {"migrated_from": "pre-D"}
        await backend.put_node(got)
        again = await backend.get_node("legacy")
        assert again is not None
        assert again.happened_at == got.happened_at
        assert again.extra == {"migrated_from": "pre-D"}
    finally:
        await backend.close()
