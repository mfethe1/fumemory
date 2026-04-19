"""SQLite-backed storage (Tier 1).

Mirrors the markdown backend's semantics but uses SQLite with FTS5 for
sub-second keyword search. Vector search plugs in via ``sqlite-vec`` when
available; if the extension cannot be loaded, :meth:`search_fts` still
works and vector calls degrade gracefully.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Literal, Optional

from .base import (
    LinkRecord,
    NodeKind,
    SearchHit,
    StorageBackend,
    WikiNode,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    tags_json   TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_json TEXT,
    agent_id    TEXT NOT NULL DEFAULT 'user',
    memory_type TEXT NOT NULL DEFAULT 'observation',
    salience    REAL NOT NULL DEFAULT 0.5,
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS nodes_kind_idx   ON nodes(kind);
CREATE INDEX IF NOT EXISTS nodes_updated_idx ON nodes(updated_at);

CREATE TABLE IF NOT EXISTS slug_registry (
    slug    TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    src_id    TEXT NOT NULL,
    dst_slug  TEXT NOT NULL,
    type      TEXT NOT NULL DEFAULT 'related',
    strength  REAL NOT NULL DEFAULT 0.5,
    PRIMARY KEY (src_id, dst_slug, type)
);

CREATE INDEX IF NOT EXISTS links_dst_idx ON links(dst_slug);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    slug, title, body, tags,
    content='nodes', content_rowid='rowid', tokenize='porter'
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, slug, title, body, tags)
    VALUES (new.rowid, new.slug, new.title, new.body, new.tags_json);
END;
CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, slug, title, body, tags)
    VALUES('delete', old.rowid, old.slug, old.title, old.body, old.tags_json);
END;
CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, slug, title, body, tags)
    VALUES('delete', old.rowid, old.slug, old.title, old.body, old.tags_json);
    INSERT INTO nodes_fts(rowid, slug, title, body, tags)
    VALUES (new.rowid, new.slug, new.title, new.body, new.tags_json);
END;
"""


class SqliteBackend:
    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None

    # ---- lifecycle
    async def init(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._conn.executescript(FTS_TRIGGERS)
        self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteBackend.init() must be called before use")
        return self._conn

    # ---- node CRUD
    async def put_node(self, node: WikiNode) -> WikiNode:
        now = datetime.now(timezone.utc)
        if node.created_at is None:
            node.created_at = now
        node.updated_at = now
        self.conn.execute(
            """
            INSERT INTO nodes(id, slug, kind, title, body, tags_json, metadata_json,
                              source_json, agent_id, memory_type, salience,
                              confidence, created_at, updated_at)
            VALUES(:id,:slug,:kind,:title,:body,:tags,:meta,:source,:agent,:mtype,
                   :sal,:conf,:created,:updated)
            ON CONFLICT(id) DO UPDATE SET
                slug=excluded.slug,
                kind=excluded.kind,
                title=excluded.title,
                body=excluded.body,
                tags_json=excluded.tags_json,
                metadata_json=excluded.metadata_json,
                source_json=excluded.source_json,
                agent_id=excluded.agent_id,
                memory_type=excluded.memory_type,
                salience=excluded.salience,
                confidence=excluded.confidence,
                updated_at=excluded.updated_at
            """,
            {
                "id": node.id,
                "slug": node.slug,
                "kind": node.kind,
                "title": node.title,
                "body": node.body,
                "tags": json.dumps(list(node.tags)),
                "meta": json.dumps(node.metadata or {}),
                "source": json.dumps(node.source) if node.source else None,
                "agent": node.agent_id,
                "mtype": node.memory_type,
                "sal": node.salience,
                "conf": node.confidence,
                "created": node.created_at.isoformat(),
                "updated": node.updated_at.isoformat(),
            },
        )
        await self.register_slug(node.slug, node.id, node.kind)
        # Replace outbound links atomically.
        self.conn.execute("DELETE FROM links WHERE src_id = ?", (node.id,))
        for link in node.links:
            self.conn.execute(
                """INSERT OR REPLACE INTO links(src_id, dst_slug, type, strength)
                   VALUES (?, ?, ?, ?)""",
                (node.id, link.dst_slug, link.type, link.strength),
            )
        self.conn.commit()
        return node

    async def get_node(self, ref: str) -> Optional[WikiNode]:
        row = self.conn.execute(
            "SELECT * FROM nodes WHERE id = ? OR slug = ? LIMIT 1", (ref, ref)
        ).fetchone()
        return self._row_to_node(row) if row else None

    async def delete_node(self, ref: str) -> None:
        self.conn.execute(
            "DELETE FROM nodes WHERE id = ? OR slug = ?", (ref, ref)
        )
        self.conn.commit()

    async def list_nodes(
        self, kind: Optional[NodeKind] = None, limit: int = 100
    ) -> list[WikiNode]:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE kind = ? ORDER BY updated_at DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM nodes ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # ---- slug registry
    async def register_slug(self, slug: str, node_id: str, kind: NodeKind) -> None:
        self.conn.execute(
            """INSERT INTO slug_registry(slug, node_id, kind)
               VALUES (?, ?, ?)
               ON CONFLICT(slug) DO UPDATE SET node_id=excluded.node_id,
                                              kind=excluded.kind""",
            (slug, node_id, kind),
        )
        self.conn.commit()

    async def resolve_slug(self, slug: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT node_id FROM slug_registry WHERE slug = ?", (slug,)
        ).fetchone()
        return row["node_id"] if row else None

    # ---- links
    async def put_link(self, link: LinkRecord) -> None:
        src_id = link.src_id or await self.resolve_slug(link.src_slug)
        if src_id is None:
            raise ValueError(f"Unknown src slug: {link.src_slug!r}")
        self.conn.execute(
            """INSERT OR REPLACE INTO links(src_id, dst_slug, type, strength)
               VALUES (?, ?, ?, ?)""",
            (src_id, link.dst_slug, link.type, link.strength),
        )
        self.conn.commit()

    async def list_links(
        self,
        node_ref: str,
        direction: Literal["out", "in", "both"] = "both",
    ) -> list[LinkRecord]:
        node = await self.get_node(node_ref)
        if node is None:
            return []
        out: list[LinkRecord] = []
        if direction in {"out", "both"}:
            rows = self.conn.execute(
                "SELECT src_id, dst_slug, type, strength FROM links WHERE src_id = ?",
                (node.id,),
            ).fetchall()
            out.extend(
                LinkRecord(
                    src_slug=node.slug,
                    dst_slug=r["dst_slug"],
                    type=r["type"],
                    strength=r["strength"],
                    src_id=r["src_id"],
                )
                for r in rows
            )
        if direction in {"in", "both"}:
            rows = self.conn.execute(
                """SELECT n.slug AS src_slug, n.id AS src_id, l.dst_slug, l.type, l.strength
                   FROM links l JOIN nodes n ON n.id = l.src_id
                   WHERE l.dst_slug = ?""",
                (node.slug,),
            ).fetchall()
            out.extend(
                LinkRecord(
                    src_slug=r["src_slug"],
                    dst_slug=r["dst_slug"],
                    type=r["type"],
                    strength=r["strength"],
                    src_id=r["src_id"],
                )
                for r in rows
            )
        return out

    # ---- search
    async def search_fts(
        self, query: str, k: int = 10, kind: Optional[NodeKind] = None
    ) -> list[SearchHit]:
        if not query.strip():
            return []
        # Quote to disable FTS5 operators for safety with arbitrary user input.
        escaped = '"' + query.replace('"', '""') + '"'
        if kind:
            rows = self.conn.execute(
                """SELECT n.*, bm25(nodes_fts) AS score
                   FROM nodes_fts JOIN nodes n ON n.rowid = nodes_fts.rowid
                   WHERE nodes_fts MATCH ? AND n.kind = ?
                   ORDER BY score LIMIT ?""",
                (escaped, kind, k),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT n.*, bm25(nodes_fts) AS score
                   FROM nodes_fts JOIN nodes n ON n.rowid = nodes_fts.rowid
                   WHERE nodes_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (escaped, k),
            ).fetchall()
        return [
            SearchHit(node=self._row_to_node(r), score=-float(r["score"]), reason="fts")
            for r in rows
        ]

    # ---- iteration
    async def iter_changed(
        self, since: Optional[datetime] = None
    ) -> AsyncIterator[WikiNode]:
        if since is not None:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE updated_at >= ? ORDER BY updated_at",
                (since.isoformat(),),
            )
        else:
            rows = self.conn.execute("SELECT * FROM nodes ORDER BY updated_at")
        for row in rows:
            yield self._row_to_node(row)

    # ---- helpers
    def _row_to_node(self, row: sqlite3.Row) -> WikiNode:
        src_id = row["id"]
        link_rows = self.conn.execute(
            "SELECT dst_slug, type, strength FROM links WHERE src_id = ?", (src_id,)
        ).fetchall()
        return WikiNode(
            id=row["id"],
            slug=row["slug"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            tags=json.loads(row["tags_json"] or "[]"),
            metadata=json.loads(row["metadata_json"] or "{}"),
            source=json.loads(row["source_json"]) if row["source_json"] else None,
            agent_id=row["agent_id"],
            memory_type=row["memory_type"],
            salience=row["salience"],
            confidence=row["confidence"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
            links=[
                LinkRecord(
                    src_slug=row["slug"],
                    dst_slug=lr["dst_slug"],
                    type=lr["type"],
                    strength=lr["strength"],
                    src_id=src_id,
                )
                for lr in link_rows
            ],
        )


def _parse(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None
