from __future__ import annotations

import asyncio
import uuid

from memu.rlm import Orchestrator, RLMContext
from memu.storage import get_backend
from memu.storage.base import WikiNode


def _node(slug: str, body: str) -> WikiNode:
    return WikiNode(
        id=str(uuid.uuid4()), slug=slug, kind="note", title=slug, body=body
    )


def test_orchestrator_cites_only_retrieved_slugs(tmp_path):
    async def go():
        backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
        await backend.init()
        for slug, body in (
            ("foo-parser", "parses foo input with quirks"),
            ("bar-util", "unrelated utility"),
            ("foo-design", "foo parser design decisions"),
        ):
            await backend.put_node(_node(slug, body))

        async def deterministic_decompose(task: str, fanout: int) -> list[str]:
            return ["foo parser", "parser design"]

        orch = Orchestrator(backend, decomposer=deterministic_decompose)
        result = await orch.solve("fix foo parser", RLMContext(max_depth=2, k=4))

        # Every cited slug must exist in the backend.
        for slug in result.slugs:
            assert await backend.resolve_slug(slug) is not None

        # Answer must be composed from citations.
        assert "[[foo-parser]]" in result.answer or "[[foo-design]]" in result.answer

        await backend.close()

    asyncio.run(go())


def test_orchestrator_respects_max_depth(tmp_path):
    async def go():
        backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
        await backend.init()
        await backend.put_node(_node("only", "only relevant note about signals"))

        orch = Orchestrator(backend)
        result = await orch.solve("signals", RLMContext(max_depth=1, k=3))
        assert result.slugs == ["only"]

        await backend.close()

    asyncio.run(go())
