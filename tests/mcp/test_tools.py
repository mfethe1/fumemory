from __future__ import annotations

import asyncio

from memu.mcp import tools
from memu.storage import get_backend


def test_tool_schemas_are_well_formed():
    assert len(tools.TOOL_SCHEMAS) >= 5
    for schema in tools.TOOL_SCHEMAS:
        assert "name" in schema
        assert "description" in schema
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"


def test_wiki_write_then_search(tmp_path):
    async def go():
        backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
        await backend.init()

        write_result = await tools.dispatch(
            backend,
            "wiki_write",
            {
                "slug": "quick",
                "title": "Quick",
                "body": "quick brown fox cites [[beta]]",
                "kind": "note",
            },
        )
        assert write_result["slug"] == "quick"
        assert write_result["outbound_links"] == 1

        search = await tools.dispatch(backend, "wiki_search", {"query": "quick", "k": 3})
        slugs = [h["slug"] for h in search["hits"]]
        assert "quick" in slugs

        read = await tools.dispatch(backend, "wiki_read", {"ref": "quick"})
        assert read["slug"] == "quick"
        assert any(l["dst_slug"] == "beta" for l in read["outbound"])

        await backend.close()

    asyncio.run(go())


def test_concurrent_wiki_writes_stay_consistent(tmp_path):
    """Many concurrent wiki_write calls on one slug must leave body and
    outbound edges in a coherent pairing — never a body from write N with
    edges derived from write M."""

    async def go():
        backend = get_backend(f"sqlite:///{tmp_path / 'c.db'}")
        await backend.init()

        # Each writer's body embeds its own index as both text and wikilink,
        # so body and outbound-edge set are perfectly entangled per-write.
        n = 20

        async def writer(i: int):
            return await tools.dispatch(
                backend,
                "wiki_write",
                {
                    "slug": "target",
                    "title": f"Target {i}",
                    "body": f"version {i} cites [[peer-{i}]]",
                    "kind": "note",
                },
            )

        await asyncio.gather(*(writer(i) for i in range(n)))

        read = await tools.dispatch(backend, "wiki_read", {"ref": "target"})
        body = read["body"]
        outbound_slugs = [l["dst_slug"] for l in read["outbound"]]

        # Extract which writer's body we landed on and confirm the edge set
        # matches exactly that writer's wikilink.
        assert body.startswith("version ")
        winner = body.split()[1]
        assert outbound_slugs == [f"peer-{winner}"], (
            f"body said version {winner} but outbound={outbound_slugs}"
        )

        await backend.close()

    asyncio.run(go())


def test_rlm_solve_cites_slugs(tmp_path):
    async def go():
        backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
        await backend.init()
        await tools.dispatch(
            backend,
            "wiki_write",
            {"slug": "a", "body": "alpha signal channel", "kind": "note"},
        )
        await tools.dispatch(
            backend,
            "wiki_write",
            {"slug": "b", "body": "beta signal channel", "kind": "note"},
        )

        result = await tools.dispatch(
            backend, "rlm_solve", {"task": "signal", "max_depth": 1, "k": 5}
        )
        assert set(result["slugs"]) >= {"a", "b"}
        assert "[[a]]" in result["answer"] or "[[b]]" in result["answer"]

        await backend.close()

    asyncio.run(go())
