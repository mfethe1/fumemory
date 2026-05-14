from __future__ import annotations

import asyncio
import uuid


from memu.storage import get_backend
from memu.storage.base import LinkRecord, WikiNode


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def _node(slug: str, body: str = "body", links=None) -> WikiNode:
    return WikiNode(
        id=str(uuid.uuid4()),
        slug=slug,
        kind="note",
        title=slug,
        body=body,
        links=list(links or []),
    )


def test_markdown_roundtrip(tmp_path):
    async def go():
        backend = get_backend(f"file://{tmp_path}")
        await backend.init()
        node = _node("foo", body="# Foo\nmentions [[bar]]\n")
        await backend.put_node(node)

        got = await backend.get_node("foo")
        assert got is not None
        assert got.id == node.id
        assert got.slug == "foo"
        assert "[[bar]]" in got.body
        await backend.close()

    _run(go())


def test_markdown_search(tmp_path):
    async def go():
        backend = get_backend(f"file://{tmp_path}")
        await backend.init()
        await backend.put_node(_node("alpha", body="quick brown fox"))
        await backend.put_node(_node("beta", body="quick lazy dog"))
        await backend.put_node(_node("gamma", body="nothing relevant"))

        hits = await backend.search_fts("quick", k=5)
        slugs = {h.node.slug for h in hits}
        assert {"alpha", "beta"}.issubset(slugs)
        await backend.close()

    _run(go())


def test_markdown_links_in_and_out(tmp_path):
    async def go():
        backend = get_backend(f"file://{tmp_path}")
        await backend.init()
        src = _node("src", links=[LinkRecord(src_slug="src", dst_slug="dst", type="related")])
        dst = _node("dst")
        await backend.put_node(dst)
        await backend.put_node(src)

        out = await backend.list_links("src", direction="out")
        assert [link.dst_slug for link in out] == ["dst"]
        inbound = await backend.list_links("dst", direction="in")
        assert [link.src_slug for link in inbound] == ["src"]
        await backend.close()

    _run(go())
