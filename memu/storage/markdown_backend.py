"""Filesystem-only backend: markdown files are source-of-truth.

Tier 0 of the storage stack. Requires no database and no external services.
Search falls back to ripgrep-style scans; for heavier retrieval, pair this
backend with :class:`SqliteBackend` via :class:`DualBackend`.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Literal, Optional

from ..wiki.frontmatter import dump_frontmatter, parse_frontmatter
from ..wiki.vault import VaultLayout
from .base import (
    LinkRecord,
    NodeKind,
    SearchHit,
    WikiNode,
)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = _SLUG_RE.sub("-", text)
    return text.strip("-") or "untitled"


class MarkdownBackend:
    """Filesystem backend. One markdown file per node."""

    capabilities = frozenset({"fts"})

    def __init__(self, root: str | os.PathLike[str]):
        self.layout = VaultLayout(Path(root))

    # ------------------------------------------------------------------ init
    async def init(self) -> None:
        self.layout.ensure()

    # ----------------------------------------------------------------- paths
    def _path_for(self, node: WikiNode) -> Path:
        return self.layout.path_for(kind=node.kind, slug=node.slug)

    def _all_md(self) -> list[Path]:
        return [p for p in self.layout.root.rglob("*.md") if "_index" not in p.parts]

    # ----------------------------------------------------------------- CRUD
    async def put_node(
        self,
        node: WikiNode,
        *,
        fencing_token: int | None = None,
    ) -> WikiNode:
        # TODO(neighborhood-lock): enforce fencing_token against
        # ``.memu/locks/_fence/<slug>`` (see NEIGHBORHOOD_LOCK_DESIGN §5).
        # The token is accepted-and-ignored for now so callers can
        # thread it uniformly across tiers; SQLite is the tier that
        # actively enforces it in this PR.
        del fencing_token
        now = datetime.now(timezone.utc)
        # Reinforcement-on-duplicate: same slug + same content_hash() =>
        # bump the counter instead of rewriting. Preserves the original
        # ``created_at`` and body bytes. Different body resets the
        # counter (a new generation of the memory).
        existing = await self._find_by_slug(node.slug)
        if existing is not None and existing.content_hash() == node.content_hash():
            # Reinforcement preserves title+body (that's what equivalence
            # means) but still refreshes side-channel fields the caller
            # may have set on the incoming node (``happened_at``,
            # ``metadata``, ``extra``, ``tags``, ``source``, ``agent_id``,
            # etc.). Without this, callers who re-put a known-good node
            # after enriching its metadata would silently lose the
            # enrichment.
            existing.reinforcement_count = (existing.reinforcement_count or 0) + 1
            existing.last_reinforced_at = now
            existing.updated_at = now
            existing.tags = list(node.tags)
            existing.metadata = dict(node.metadata or {})
            existing.extra = dict(node.extra or {})
            existing.source = node.source
            existing.happened_at = node.happened_at
            existing.agent_id = node.agent_id
            existing.memory_type = node.memory_type
            existing.salience = node.salience
            existing.confidence = node.confidence
            path = self._path_for(existing)
            path.parent.mkdir(parents=True, exist_ok=True)
            fm = existing.to_frontmatter()
            body = existing.body or f"# {existing.title}\n"
            path.write_text(dump_frontmatter(fm, body), encoding="utf-8")
            return existing
        if existing is not None:
            node.reinforcement_count = 0
            node.last_reinforced_at = None
        node.updated_at = now
        if node.created_at is None:
            node.created_at = now
        path = self._path_for(node)
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = node.to_frontmatter()
        body = node.body or f"# {node.title}\n"
        path.write_text(dump_frontmatter(fm, body), encoding="utf-8")
        return node

    async def reinforce_node(self, ref: str) -> Optional[WikiNode]:
        node = await self.get_node(ref)
        if node is None:
            return None
        now = datetime.now(timezone.utc)
        node.reinforcement_count = (node.reinforcement_count or 0) + 1
        node.last_reinforced_at = now
        node.updated_at = now
        path = self._path_for(node)
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = node.to_frontmatter()
        body = node.body or f"# {node.title}\n"
        path.write_text(dump_frontmatter(fm, body), encoding="utf-8")
        return node

    async def get_node(self, ref: str) -> Optional[WikiNode]:
        # Try slug first (cheap), then id (requires scan).
        by_slug = await self._find_by_slug(ref)
        if by_slug is not None:
            return by_slug
        return await self._find_by_id(ref)

    async def _find_by_slug(self, slug: str) -> Optional[WikiNode]:
        for path in self._all_md():
            node = self._read(path)
            if node and node.slug == slug:
                return node
        return None

    async def _find_by_id(self, node_id: str) -> Optional[WikiNode]:
        for path in self._all_md():
            node = self._read(path)
            if node and node.id == node_id:
                return node
        return None

    async def delete_node(self, ref: str) -> None:
        node = await self.get_node(ref)
        if node is None:
            return
        self._path_for(node).unlink(missing_ok=True)

    async def list_nodes(
        self, kind: Optional[NodeKind] = None, limit: int = 100
    ) -> list[WikiNode]:
        out: list[WikiNode] = []
        for path in self._all_md():
            node = self._read(path)
            if node is None:
                continue
            if kind and node.kind != kind:
                continue
            out.append(node)
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------ slug reg.
    async def register_slug(self, slug: str, node_id: str, kind: NodeKind) -> None:
        # Markdown backend: slug lives in frontmatter, so this is a no-op.
        return None

    async def resolve_slug(self, slug: str) -> Optional[str]:
        node = await self._find_by_slug(slug)
        return node.id if node else None

    # ----------------------------------------------------------------- links
    async def put_link(self, link: LinkRecord) -> None:
        src = await self.get_node(link.src_slug)
        if src is None:
            return
        # De-duplicate by (dst_slug, type)
        key = (link.dst_slug, link.type)
        src.links = [existing for existing in src.links if (existing.dst_slug, existing.type) != key]
        src.links.append(link)
        await self.put_node(src)

    async def list_links(
        self,
        node_ref: str,
        direction: Literal["out", "in", "both"] = "both",
    ) -> list[LinkRecord]:
        out: list[LinkRecord] = []
        src = await self.get_node(node_ref)
        if src and direction in {"out", "both"}:
            out.extend(src.links)
        if direction in {"in", "both"}:
            target_slug = src.slug if src else node_ref
            for path in self._all_md():
                other = self._read(path)
                if other is None or (src and other.id == src.id):
                    continue
                for link in other.links:
                    if link.dst_slug == target_slug:
                        out.append(LinkRecord(
                            src_slug=other.slug,
                            dst_slug=target_slug,
                            type=link.type,
                            strength=link.strength,
                            src_id=other.id,
                        ))
        return out

    # ---------------------------------------------------------------- search
    async def search_fts(
        self, query: str, k: int = 10, kind: Optional[NodeKind] = None
    ) -> list[SearchHit]:
        needle = query.lower().strip()
        if not needle:
            return []
        hits: list[SearchHit] = []
        for node in await self.list_nodes(kind=kind, limit=10_000):
            haystack = (node.title + "\n" + node.body).lower()
            score = haystack.count(needle)
            if score:
                hits.append(SearchHit(node=node, score=float(score), reason="fts"))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    # ------------------------------------------------------------- iteration
    async def iter_changed(
        self, since: Optional[datetime] = None
    ) -> AsyncIterator[WikiNode]:
        for path in self._all_md():
            if since is not None:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if mtime < since:
                    continue
            node = self._read(path)
            if node:
                yield node
            await asyncio.sleep(0)

    async def close(self) -> None:  # nothing to close
        return None

    def get_lock_registry(self):
        """Return a :class:`MarkdownLockRegistry` rooted at this vault.
        Callers must ``await registry.init()`` before use."""
        from ..neighborhood_lock import MarkdownLockRegistry

        return MarkdownLockRegistry(root=self.layout.root)

    # ---------------------------------------------------------------- helpers
    def _read(self, path: Path) -> Optional[WikiNode]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            fm, body = parse_frontmatter(raw)
        except Exception:
            return None
        if not fm or not fm.get("id") or not fm.get("slug"):
            return None
        links = [
            LinkRecord(
                src_slug=fm["slug"],
                dst_slug=link_data.get("slug", ""),
                type=link_data.get("type", "related"),
                strength=float(link_data.get("strength", 0.5)),
            )
            for link_data in fm.get("links", [])
            if link_data.get("slug")
        ]
        return WikiNode(
            id=str(fm["id"]),
            slug=str(fm["slug"]),
            kind=fm.get("kind", "note"),
            title=fm.get("title") or path.stem,
            body=body,
            tags=list(fm.get("tags") or []),
            links=links,
            metadata=fm.get("metadata") or {},
            created_at=_parse_dt(fm.get("created")),
            updated_at=_parse_dt(fm.get("updated")),
            happened_at=_parse_dt(fm.get("happened_at")),
            salience=float(fm.get("salience", 0.5)),
            confidence=float(fm.get("confidence", 1.0)),
            agent_id=fm.get("agent_id", "user"),
            memory_type=fm.get("memory_type", "observation"),
            source=fm.get("source"),
            extra=dict(fm.get("extra") or {}),
            reinforcement_count=int(fm.get("reinforcement_count") or 0),
            last_reinforced_at=_parse_dt(fm.get("last_reinforced_at")),
        )


def _parse_dt(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None
