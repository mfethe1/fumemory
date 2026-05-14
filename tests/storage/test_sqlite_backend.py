from __future__ import annotations

import asyncio
import uuid

from memu.storage import get_backend
from memu.storage.base import LinkRecord, WikiNode


def _node(slug: str, body: str = "body", links=None, kind="note") -> WikiNode:
    return WikiNode(
        id=str(uuid.uuid4()),
        slug=slug,
        kind=kind,
        title=slug,
        body=body,
        links=list(links or []),
    )


def test_sqlite_crud_and_fts(tmp_path):
    async def go():
        db = tmp_path / "index.db"
        backend = get_backend(f"sqlite:///{db}")
        await backend.init()

        a = _node("alpha", body="the quick brown fox")
        b = _node(
            "beta",
            body="a quick lazy dog; see [[alpha]]",
            links=[LinkRecord(src_slug="beta", dst_slug="alpha", type="related")],
        )
        await backend.put_node(a)
        await backend.put_node(b)

        # Read back
        got = await backend.get_node("alpha")
        assert got is not None and got.slug == "alpha"

        # FTS search
        hits = await backend.search_fts("quick", k=5)
        assert {h.node.slug for h in hits} == {"alpha", "beta"}

        # Outbound + inbound links
        out = await backend.list_links("beta", direction="out")
        assert [link.dst_slug for link in out] == ["alpha"]
        inbound = await backend.list_links("alpha", direction="in")
        assert [link.src_slug for link in inbound] == ["beta"]

        # Kind filter
        hits_code = await backend.search_fts("quick", k=5, kind="code")
        assert hits_code == []

        await backend.close()

    asyncio.run(go())


def test_sqlite_slug_registry(tmp_path):
    async def go():
        db = tmp_path / "index.db"
        backend = get_backend(f"sqlite:///{db}")
        await backend.init()

        node = _node("slug-one")
        await backend.put_node(node)

        resolved = await backend.resolve_slug("slug-one")
        assert resolved == node.id

        assert await backend.resolve_slug("nonexistent") is None

        await backend.close()

    asyncio.run(go())
