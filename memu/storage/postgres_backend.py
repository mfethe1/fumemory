"""PostgreSQL backend (Tier 2/3).

Phase 5 deliverable. Keeps the StorageBackend protocol surface frozen so
Claude Code / Cursor / MCP clients do not care whether the vault is backed
by SQLite or Postgres+pgvector+AGE.

This file ships as a stub: instantiating it raises
``NotImplementedError`` with guidance, while the shape (constructor + attr)
is already in place for the DualBackend cutover tool.
"""
from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator, Literal, Optional

from .base import (
    LinkRecord,
    NodeKind,
    SearchHit,
    StorageBackend,
    WikiNode,
)


class PostgresBackend:
    """Placeholder until Phase 5.

    The Phase 5 PR will port the SQL currently embedded in ``memu/api.py``
    into this class, reusing the schema in ``memu/migrations/000*`` and
    ``002*``. Until then, running memU against a ``postgres://`` DSN still
    routes through the legacy FastAPI path.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn

    async def init(self) -> None:
        raise NotImplementedError(
            "PostgresBackend is pending Phase 5. Until it lands, keep using "
            "the FastAPI routes in memu/api.py (they already speak Postgres) "
            "or set MEMU_STORAGE_DSN to a sqlite:/// or file:// DSN."
        )

    async def put_node(self, node: WikiNode) -> WikiNode:  # pragma: no cover - stub
        raise NotImplementedError

    async def get_node(self, ref: str) -> Optional[WikiNode]:  # pragma: no cover
        raise NotImplementedError

    async def delete_node(self, ref: str) -> None:  # pragma: no cover
        raise NotImplementedError

    async def list_nodes(self, kind: Optional[NodeKind] = None, limit: int = 100) -> list[WikiNode]:  # pragma: no cover
        raise NotImplementedError

    async def register_slug(self, slug: str, node_id: str, kind: NodeKind) -> None:  # pragma: no cover
        raise NotImplementedError

    async def resolve_slug(self, slug: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    async def put_link(self, link: LinkRecord) -> None:  # pragma: no cover
        raise NotImplementedError

    async def list_links(self, node_ref: str, direction: Literal["out", "in", "both"] = "both") -> list[LinkRecord]:  # pragma: no cover
        raise NotImplementedError

    async def search_fts(self, query: str, k: int = 10, kind: Optional[NodeKind] = None) -> list[SearchHit]:  # pragma: no cover
        raise NotImplementedError

    async def iter_changed(self, since: Optional[datetime] = None) -> AsyncIterator[WikiNode]:  # pragma: no cover
        raise NotImplementedError
        if False:  # pragma: no cover
            yield  # type: ignore[misc]

    def get_lock_registry(self):  # pragma: no cover - stub
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        return None
