"""Slug <-> id resolution, backed by whichever StorageBackend is active.

Also materializes *placeholder* nodes for dangling wikilinks so that agents
can still reference them and the vault_doctor can surface them for cleanup.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from ..storage.base import NodeKind, StorageBackend, WikiNode


class SlugRegistry:
    def __init__(self, backend: StorageBackend):
        self.backend = backend

    async def resolve(self, slug: str) -> Optional[str]:
        return await self.backend.resolve_slug(slug)

    async def resolve_or_stub(
        self,
        slug: str,
        *,
        kind: NodeKind = "note",
        title: Optional[str] = None,
        agent_id: str = "memu",
    ) -> str:
        node_id = await self.backend.resolve_slug(slug)
        if node_id:
            return node_id
        stub = WikiNode(
            id=str(uuid.uuid4()),
            slug=slug,
            kind=_infer_kind(slug, kind),
            title=title or slug,
            body=f"# {title or slug}\n\n> Placeholder stub — materialized by memU.\n",
            agent_id=agent_id,
            memory_type="placeholder",
            confidence=0.0,
            salience=0.1,
            created_at=datetime.now(timezone.utc),
        )
        await self.backend.put_node(stub)
        return stub.id


def _infer_kind(slug: str, default: NodeKind) -> NodeKind:
    if slug.startswith("paper/"):
        return "paper"
    if slug.startswith("code/"):
        return "code"
    if slug.startswith("task/") or slug.startswith("tasks/"):
        return "task"
    return default
