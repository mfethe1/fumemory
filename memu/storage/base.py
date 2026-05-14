"""Storage backend protocol and shared DTOs.

The same protocol is implemented by :mod:`markdown_backend`,
:mod:`sqlite_backend`, and :mod:`postgres_backend`. Agents, the MCP server,
and the RLM orchestrator only talk to this interface, so storage can be
swapped without touching business logic.
"""
from __future__ import annotations

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


NodeKind = Literal["note", "code", "paper", "task"]
LinkType = Literal[
    "similar", "extends", "contradicts", "supersedes", "caused_by", "related"
]


@dataclass
class WikiNode:
    """A single addressable memory node.

    A node maps 1:1 to a markdown file in the vault. ``id`` is globally
    stable (ULID/UUID); ``slug`` is the human/Obsidian-friendly handle.
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
    salience: float = 0.5
    confidence: float = 1.0
    agent_id: str = "user"
    memory_type: str = "observation"
    source: Optional[dict[str, Any]] = None  # {path, symbol, commit} for code/paper nodes

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
        return fm


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
    async def put_node(self, node: WikiNode) -> WikiNode: ...
    async def get_node(self, ref: str) -> Optional[WikiNode]:
        """Fetch by id or slug. Backends should accept either."""

    async def delete_node(self, ref: str) -> None: ...

    async def list_nodes(
        self, kind: Optional[NodeKind] = None, limit: int = 100
    ) -> list[WikiNode]: ...

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
