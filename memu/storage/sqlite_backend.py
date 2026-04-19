"""SQLite-backed storage (Tier 1).

Mirrors the markdown backend's semantics but uses SQLite with FTS5 for
sub-second keyword search. Vector search plugs in via ``sqlite-vec`` when
available; if the extension cannot be loaded, :meth:`search_fts` still
works and vector calls degrade gracefully.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Literal, Optional

from ..lane_lock import FencingTokenError
from .base import (
    LinkRecord,
    NodeKind,
    SearchHit,
    StorageBackend,
    WikiNode,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id                  TEXT PRIMARY KEY,
    slug                TEXT UNIQUE NOT NULL,
    kind                TEXT NOT NULL,
    title               TEXT NOT NULL,
    body                TEXT NOT NULL,
    tags_json           TEXT NOT NULL DEFAULT '[]',
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    extra_json          TEXT NOT NULL DEFAULT '{}',
    source_json         TEXT,
    agent_id            TEXT NOT NULL DEFAULT 'user',
    memory_type         TEXT NOT NULL DEFAULT 'observation',
    salience            REAL NOT NULL DEFAULT 0.5,
    confidence          REAL NOT NULL DEFAULT 1.0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    happened_at         TEXT,
    reinforcement_count INTEGER NOT NULL DEFAULT 0,
    last_reinforced_at  TEXT
);

CREATE INDEX IF NOT EXISTS nodes_kind_idx       ON nodes(kind);
CREATE INDEX IF NOT EXISTS nodes_updated_idx    ON nodes(updated_at);
-- ``nodes_happened_idx`` and ``nodes_reinforce_idx`` are created by
-- ``_apply_idempotent_migrations`` so that legacy DBs without the
-- underlying columns don't error on the CREATE INDEX step before the
-- columns are added.

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

-- Neighborhood lock registry (see ``memu/neighborhood_lock.py``).
-- One row per held slug; ``root_slug`` records which acquisition owns
-- the row so release can reclaim every slug in a single neighborhood.
CREATE TABLE IF NOT EXISTS neighborhood_locks (
    slug           TEXT PRIMARY KEY,
    agent          TEXT NOT NULL,
    fencing_token  INTEGER NOT NULL,
    acquired_at    TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    root_slug      TEXT NOT NULL
);

-- Monotonic fencing-token counter, one row per slug. Survives lock
-- releases so every acquire issues a strictly-higher token.
CREATE TABLE IF NOT EXISTS fencing_tokens (
    slug   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
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
        fts_existed_before = _has_table(self._conn, "nodes_fts")
        nodes_count_before = (
            self._conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
            if _has_table(self._conn, "nodes")
            else 0
        )
        # Order matters: ALTER TABLE must precede CREATE VIRTUAL TABLE so
        # the external-content FTS5 table binds to the final ``nodes``
        # schema, not an intermediate one.
        _apply_idempotent_migrations(self._conn)
        self._conn.executescript(SCHEMA)
        self._conn.executescript(FTS_TRIGGERS)
        # If FTS was just created but ``nodes`` already held rows, the
        # insert triggers never fired for them; backfill so the AFTER
        # UPDATE trigger's shadow-row delete succeeds on first put_node.
        if not fts_existed_before and nodes_count_before > 0:
            self._conn.execute(
                "INSERT INTO nodes_fts(rowid, slug, title, body, tags) "
                "SELECT rowid, slug, title, body, tags_json FROM nodes"
            )
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
    async def put_node(
        self,
        node: WikiNode,
        *,
        fencing_token: int | None = None,
    ) -> WikiNode:
        now = datetime.now(timezone.utc)
        if node.created_at is None:
            node.created_at = now
        node.updated_at = now
        if fencing_token is not None:
            # Split-brain guard: require the caller to prove they hold
            # the current neighborhood lock. Runs in the same implicit
            # transaction as the INSERT/UPDATE below so a racing
            # acquirer can't slip between the check and the write.
            row = self.conn.execute(
                "SELECT fencing_token FROM neighborhood_locks WHERE slug = ?",
                (node.slug,),
            ).fetchone()
            if row is None:
                raise FencingTokenError(
                    f"No neighborhood lock held for slug {node.slug!r}; "
                    f"cannot validate fencing token {fencing_token}."
                )
            held = int(row["fencing_token"])
            if held != fencing_token:
                raise FencingTokenError(
                    f"Fencing token mismatch on slug {node.slug!r}: "
                    f"caller has {fencing_token}, registry has {held}. "
                    f"Split-brain prevented."
                )
        # Reinforcement-on-duplicate: if the same slug is already in the
        # index with an identical ``content_hash()``, treat this as a
        # "seen again" signal and bump the counter/timestamp instead of
        # overwriting the row. Upstream memU does this to let summaries
        # surface the most-reinforced items first.
        existing_row = self.conn.execute(
            """SELECT id, title, body, reinforcement_count, created_at
               FROM nodes WHERE slug = ? LIMIT 1""",
            (node.slug,),
        ).fetchone()
        if existing_row is not None and _row_content_hash(existing_row) == node.content_hash():
            new_count = int(existing_row["reinforcement_count"] or 0) + 1
            # Reinforcement preserves title+body (that's what equivalence
            # means) but still refreshes side-channel fields the caller
            # may have set on the incoming node (``happened_at``,
            # ``metadata``, ``extra``, ``tags``, ``source``, ``agent_id``,
            # etc.). Without this, callers who re-put a known-good node
            # after enriching its metadata would silently lose the
            # enrichment.
            self.conn.execute(
                """UPDATE nodes
                      SET reinforcement_count = ?,
                          last_reinforced_at  = ?,
                          updated_at          = ?,
                          happened_at         = ?,
                          tags_json           = ?,
                          metadata_json       = ?,
                          extra_json          = ?,
                          source_json         = ?,
                          agent_id            = ?,
                          memory_type         = ?,
                          salience            = ?,
                          confidence          = ?
                    WHERE id = ?""",
                (
                    new_count,
                    now.isoformat(),
                    now.isoformat(),
                    node.happened_at.isoformat() if node.happened_at else None,
                    json.dumps(list(node.tags)),
                    json.dumps(node.metadata or {}),
                    json.dumps(node.extra or {}),
                    json.dumps(node.source) if node.source else None,
                    node.agent_id,
                    node.memory_type,
                    node.salience,
                    node.confidence,
                    existing_row["id"],
                ),
            )
            self.conn.commit()
            # Return the stored node so the caller observes the bumped
            # counter + merged side-channel fields.
            stored = await self.get_node(existing_row["id"])
            return stored if stored is not None else node
        # Different content — this is a real write. Reset reinforcement
        # because the node has changed; upstream considers edits a new
        # "generation" of the memory.
        if existing_row is not None:
            node.reinforcement_count = 0
            node.last_reinforced_at = None
        self.conn.execute(
            """
            INSERT INTO nodes(id, slug, kind, title, body, tags_json, metadata_json,
                              extra_json, source_json, agent_id, memory_type, salience,
                              confidence, created_at, updated_at, happened_at,
                              reinforcement_count, last_reinforced_at)
            VALUES(:id,:slug,:kind,:title,:body,:tags,:meta,:extra,:source,:agent,:mtype,
                   :sal,:conf,:created,:updated,:happened,:rcount,:rat)
            ON CONFLICT(id) DO UPDATE SET
                slug=excluded.slug,
                kind=excluded.kind,
                title=excluded.title,
                body=excluded.body,
                tags_json=excluded.tags_json,
                metadata_json=excluded.metadata_json,
                extra_json=excluded.extra_json,
                source_json=excluded.source_json,
                agent_id=excluded.agent_id,
                memory_type=excluded.memory_type,
                salience=excluded.salience,
                confidence=excluded.confidence,
                updated_at=excluded.updated_at,
                happened_at=excluded.happened_at,
                reinforcement_count=excluded.reinforcement_count,
                last_reinforced_at=excluded.last_reinforced_at
            """,
            {
                "id": node.id,
                "slug": node.slug,
                "kind": node.kind,
                "title": node.title,
                "body": node.body,
                "tags": json.dumps(list(node.tags)),
                "meta": json.dumps(node.metadata or {}),
                "extra": json.dumps(node.extra or {}),
                "source": json.dumps(node.source) if node.source else None,
                "agent": node.agent_id,
                "mtype": node.memory_type,
                "sal": node.salience,
                "conf": node.confidence,
                "created": node.created_at.isoformat(),
                "updated": node.updated_at.isoformat(),
                "happened": node.happened_at.isoformat() if node.happened_at else None,
                "rcount": int(node.reinforcement_count or 0),
                "rat": node.last_reinforced_at.isoformat() if node.last_reinforced_at else None,
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

    async def reinforce_node(self, ref: str) -> Optional[WikiNode]:
        now = datetime.now(timezone.utc)
        row = self.conn.execute(
            "SELECT id FROM nodes WHERE id = ? OR slug = ? LIMIT 1",
            (ref, ref),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            """UPDATE nodes
                  SET reinforcement_count = reinforcement_count + 1,
                      last_reinforced_at  = ?,
                      updated_at          = ?
                WHERE id = ?""",
            (now.isoformat(), now.isoformat(), row["id"]),
        )
        self.conn.commit()
        return await self.get_node(row["id"])

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
        escaped = _escape_fts_query(query)
        if not escaped:
            return []
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
        extra_json = _column_or_default(row, "extra_json", "{}")
        happened_at_val = _column_or_default(row, "happened_at", None)
        rc = _column_or_default(row, "reinforcement_count", 0) or 0
        lra = _column_or_default(row, "last_reinforced_at", None)
        return WikiNode(
            id=row["id"],
            slug=row["slug"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            tags=json.loads(row["tags_json"] or "[]"),
            metadata=json.loads(row["metadata_json"] or "{}"),
            extra=json.loads(extra_json or "{}"),
            source=json.loads(row["source_json"]) if row["source_json"] else None,
            agent_id=row["agent_id"],
            memory_type=row["memory_type"],
            salience=row["salience"],
            confidence=row["confidence"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
            happened_at=_parse(happened_at_val) if happened_at_val else None,
            reinforcement_count=int(rc),
            last_reinforced_at=_parse(lra) if lra else None,
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


def _row_content_hash(row: sqlite3.Row) -> str:
    """Content hash of a raw ``nodes`` row (title + body).

    Uses the same normalization as :meth:`WikiNode.content_hash` so
    ``put_node`` can decide reinforce-vs-overwrite without materializing
    a full ``WikiNode`` first.
    """
    # Build a throwaway WikiNode-like object just to call the method;
    # avoids duplicating the normalization logic here.
    from .base import WikiNode as _WN  # local import avoids cycle at module load
    return _WN(
        id="", slug="", kind="note",
        title=row["title"] or "",
        body=row["body"] or "",
    ).content_hash()


def _apply_idempotent_migrations(conn: sqlite3.Connection) -> None:
    """Bring older SQLite vaults forward without a separate migration tool.

    Runs before ``SCHEMA`` so the external-content FTS5 table in ``SCHEMA``
    binds to the final ``nodes`` layout. For a brand-new DB the
    ``nodes`` table does not yet exist; we no-op in that case and
    ``SCHEMA``'s ``CREATE TABLE`` includes the columns directly.

    Each migration entry is ``(column, DDL)``; we add the column only
    if it is missing from ``PRAGMA table_info(nodes)``. This lets solo
    users upgrade memU in place — the first ``init()`` after a pull
    adds any new columns without destroying their index.
    """
    cur = conn.execute("PRAGMA table_info(nodes)")
    existing = {row[1] for row in cur.fetchall()}
    if not existing:
        return  # fresh DB; SCHEMA will create everything
    migrations: list[tuple[str, str]] = [
        ("extra_json", "ALTER TABLE nodes ADD COLUMN extra_json TEXT NOT NULL DEFAULT '{}'"),
        ("happened_at", "ALTER TABLE nodes ADD COLUMN happened_at TEXT"),
        (
            "reinforcement_count",
            "ALTER TABLE nodes ADD COLUMN reinforcement_count INTEGER NOT NULL DEFAULT 0",
        ),
        ("last_reinforced_at", "ALTER TABLE nodes ADD COLUMN last_reinforced_at TEXT"),
    ]
    for column, ddl in migrations:
        if column not in existing:
            conn.execute(ddl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS nodes_happened_idx ON nodes(happened_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS nodes_reinforce_idx ON nodes(reinforcement_count)"
    )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_or_default(row: sqlite3.Row, name: str, default):
    """Tolerate databases created before the column was added.

    Rather than requiring users to wipe their SQLite index when we add a
    column, the backend's ``init`` runs idempotent ``ALTER TABLE`` statements
    and this helper guards reads. Keeps upgrades painless for solo users.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-]*")


def _escape_fts_query(query: str) -> str:
    """Turn free-form user input into an FTS5 query string.

    Splits on non-identifier characters, wraps each surviving token in
    double-quotes, and joins with explicit ``OR`` so BM25 ranks the
    document by how many tokens it covers. This mirrors the feel of
    Obsidian/Google-style search — multi-word queries with stopwords
    still return relevant results ranked by overlap instead of requiring
    every token to appear. The surrounding quotes disable the FTS5
    operator surface (``NEAR``, ``*``, column filters) against
    unsanitised input.
    """
    tokens = _FTS_TOKEN_RE.findall(query)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)
