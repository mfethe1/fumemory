"""Storage backend protocol and shared DTOs.

The same protocol is implemented by :mod:`markdown_backend`,
:mod:`sqlite_backend`, and :mod:`postgres_backend`. Agents, the MCP server,
and the RLM orchestrator only talk to this interface, so storage can be
swapped without touching business logic.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Any,
    AsyncIterator,
    Literal,
    Optional,
    Protocol,
    runtime_checkable,
)


_WS_RE = re.compile(r"\s+")


def _normalize_for_hash(text: str) -> str:
    """Whitespace/case-insensitive normalization for content hashing.

    Collapses CRLF/LF/tabs/runs of spaces into single spaces, strips
    leading/trailing whitespace, and lowercases. Storage retains the
    original text — normalization is only for the hash itself.
    """
    return _WS_RE.sub(" ", text.replace("\r\n", "\n")).strip().lower()


NodeKind = Literal["note", "code", "paper", "task"]
LinkType = Literal[
    "similar", "extends", "contradicts", "supersedes", "caused_by", "related"
]


@dataclass
class WikiNode:
    """A single addressable memory node.

    A node maps 1:1 to a markdown file in the vault. ``id`` is globally
    stable (ULID/UUID); ``slug`` is the human/Obsidian-friendly handle.

    Event time vs. ingest time
    --------------------------
    ``created_at`` and ``updated_at`` track when we learned about the
    node (ingest time). ``happened_at`` is when the event the node
    describes actually occurred — often earlier, sometimes much earlier
    when importing a backlog (git log, Slack export, old ADRs). Decay
    curves and recency-weighted search should prefer ``happened_at``
    when it's set, so importing yesterday's 2022 commit doesn't make
    that commit look fresh.

    ``extra`` is a forward-compatible escape hatch for per-source
    fields that don't warrant a top-level column yet (PR number,
    author, transcript URL). Prefer promoting heavily-used keys into
    typed fields over time.
    """

    id: str
    slug: str
    kind: NodeKind
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    links: list["LinkRecord"] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    happened_at: Optional[datetime] = None
    salience: float = 0.5
    confidence: float = 1.0
    agent_id: str = "user"
    memory_type: str = "observation"
    source: Optional[dict[str, Any]] = None  # {path, symbol, commit} for code/paper nodes
    extra: dict[str, Any] = field(default_factory=dict)
    reinforcement_count: int = 0
    last_reinforced_at: Optional[datetime] = None

    def content_hash(self) -> str:
        """SHA-256 over normalized body+title.

        Stable under whitespace-only edits (one extra newline, trailing
        spaces, CRLF vs LF, tab vs space, leading/trailing blanks) and
        case changes. Changes when title or the textual body changes.

        Distinct from ``metadata['source_hash']`` used by the codebase
        ingester (that one hashes the raw source file; this one hashes
        the rendered wiki body+title and powers reinforcement-on-dup).
        """
        norm_title = _normalize_for_hash(self.title or "")
        norm_body = _normalize_for_hash(self.body or "")
        digest = hashlib.sha256()
        digest.update(norm_title.encode("utf-8"))
        digest.update(b"\x00")  # separator so title|body cannot collide with body|title
        digest.update(norm_body.encode("utf-8"))
        return digest.hexdigest()

    def to_frontmatter(self) -> dict[str, Any]:
        """Serializable frontmatter dict (ordered for readability)."""
        fm: dict[str, Any] = {
            "id": self.id,
            "slug": self.slug,
            "kind": self.kind,
            "title": self.title,
            "tags": list(self.tags),
        }
        if self.created_at:
            fm["created"] = self.created_at.isoformat()
        if self.updated_at:
            fm["updated"] = self.updated_at.isoformat()
        if self.happened_at:
            fm["happened_at"] = self.happened_at.isoformat()
        fm["agent_id"] = self.agent_id
        fm["memory_type"] = self.memory_type
        fm["salience"] = self.salience
        fm["confidence"] = self.confidence
        if self.links:
            fm["links"] = [
                {
                    "slug": link.dst_slug,
                    "type": link.type,
                    "strength": link.strength,
                }
                for link in self.links
            ]
        if self.source:
            fm["source"] = self.source
        if self.metadata:
            fm["metadata"] = self.metadata
        if self.extra:
            fm["extra"] = self.extra
        if self.reinforcement_count:
            fm["reinforcement_count"] = self.reinforcement_count
        if self.last_reinforced_at:
            fm["last_reinforced_at"] = self.last_reinforced_at.isoformat()
        return fm

    def effective_time(self) -> Optional[datetime]:
        """``happened_at`` if set, else ``created_at``.

        This is the single point decay curves and recency-weighted
        search should call; never hand-roll the fallback elsewhere.
        """
        return self.happened_at or self.created_at


@dataclass
class LinkRecord:
    """Typed edge between two nodes. ``src_slug``/``dst_slug`` are resolved
    against the slug registry; ``dst_id`` may be ``None`` for dangling links
    until the target is materialized."""

    src_slug: str
    dst_slug: str
    type: LinkType = "related"
    strength: float = 0.5
    src_id: Optional[str] = None
    dst_id: Optional[str] = None


@dataclass
class SlugRecord:
    slug: str
    id: str
    kind: NodeKind


@dataclass
class SearchHit:
    node: WikiNode
    score: float
    reason: str = "vector"  # vector | fts | graph | slug


Capability = Literal["fts", "vector", "graph"]


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol every storage tier implements.

    All methods are async-friendly; synchronous backends (markdown) wrap
    their sync calls but still present an async interface.

    Capabilities
    ------------
    Backends declare which retrieval paths they support via
    :attr:`capabilities`. ``"fts"`` is always present; ``"vector"`` and
    ``"graph"`` are optional. Callers (like the RLM retriever) inspect
    this set to decide which ``search_*`` methods to invoke — unsupported
    methods either raise :class:`NotImplementedError` or return ``[]``.
    """

    capabilities: frozenset[Capability]

    async def init(self) -> None: ...

    # --- node CRUD ---
    async def put_node(
        self,
        node: WikiNode,
        *,
        fencing_token: int | None = None,
    ) -> WikiNode:
        """Persist ``node``.

        ``fencing_token`` is an optional guard produced by
        :class:`memu.neighborhood_lock.NeighborhoodLock`. When provided,
        the backend must verify the token matches the currently-held
        lock on ``node.slug`` (inside the same transaction that mutates
        the row) and raise :class:`memu.lane_lock.FencingTokenError` on
        mismatch. When ``None`` (default) the backend skips the check
        so existing callers stay unchanged.
        """

    async def get_node(self, ref: str) -> Optional[WikiNode]:
        """Fetch by id or slug. Backends should accept either."""

    async def delete_node(self, ref: str) -> None: ...

    async def list_nodes(
        self, kind: Optional[NodeKind] = None, limit: int = 100
    ) -> list[WikiNode]: ...

    async def reinforce_node(self, ref: str) -> Optional[WikiNode]:
        """Atomically bump ``reinforcement_count`` + ``last_reinforced_at``.

        Returns the updated node, or ``None`` if no node matches ``ref``.
        Never mutates title/body/tags/links/metadata — a pure salience
        signal for duplicate-on-write or explicit "I saw this again"
        callers. Also invoked internally by :meth:`put_node` when the
        incoming node's ``content_hash()`` matches the stored node's.
        """
        ...

    # --- slug registry ---
    async def register_slug(self, slug: str, node_id: str, kind: NodeKind) -> None: ...
    async def resolve_slug(self, slug: str) -> Optional[str]: ...

    # --- links ---
    async def put_link(self, link: LinkRecord) -> None: ...
    async def list_links(self, node_ref: str, direction: Literal["out", "in", "both"] = "both") -> list[LinkRecord]: ...

    # --- search ---
    async def search_fts(self, query: str, k: int = 10, kind: Optional[NodeKind] = None) -> list[SearchHit]: ...

    async def search_vector(
        self,
        embedding: list[float],
        k: int = 10,
        kind: Optional[NodeKind] = None,
    ) -> list[SearchHit]:
        """Nearest-neighbor search by vector similarity.

        Backends without ``"vector"`` in :attr:`capabilities` may return
        ``[]`` (preferred) or raise :class:`NotImplementedError`.
        """
        return []

    async def search_graph(
        self,
        start_slug: str,
        hops: int = 1,
        rel_types: Optional[list[str]] = None,
    ) -> list[SearchHit]:
        """K-hop neighborhood expansion from a starting slug.

        Backends without ``"graph"`` in :attr:`capabilities` may return
        ``[]``.
        """
        return []

    async def put_embedding(
        self, node_id: str, embedding: list[float], *, model: Optional[str] = None
    ) -> None:
        """Store an embedding for a node. No-op on FTS-only backends."""
        return None

    # --- iteration (for sync/migration) ---
    async def iter_changed(self, since: Optional[datetime] = None) -> AsyncIterator[WikiNode]: ...

    async def close(self) -> None: ...
