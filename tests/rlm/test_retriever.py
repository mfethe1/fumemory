from __future__ import annotations

import asyncio
import hashlib
import math
import uuid

import pytest

from memu.rlm import Embedder, HybridRetriever, NullEmbedder, RetrievalPlan
from memu.rlm.retriever import _rrf_fuse
from memu.storage import get_backend
from memu.storage.base import LinkRecord, SearchHit, WikiNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(slug: str, body: str = "body", kind: str = "note", links=None) -> WikiNode:
    return WikiNode(
        id=str(uuid.uuid4()),
        slug=slug,
        kind=kind,
        title=slug,
        body=body,
        links=list(links or []),
    )


class DeterministicEmbedder:
    """A hash-based embedder so tests are reproducible without network.

    Produces normalized vectors whose cosine similarity approximates
    token overlap — enough to verify vector retrieval flow without
    requiring a real model.
    """

    def __init__(self, dims: int = 32):
        self.dimensions = dims

    async def embed(self, text: str):
        vec = [0.0] * self.dimensions
        for token in text.lower().split():
            digest = hashlib.sha1(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


class ExplodingEmbedder:
    dimensions = 16

    async def embed(self, text: str):
        raise RuntimeError("embedding provider is down")


# ---------------------------------------------------------------------------
# RRF fusion (pure function)
# ---------------------------------------------------------------------------


def test_rrf_fuses_multiple_lists_and_rewards_cross_list_overlap():
    a = _node("a")
    b = _node("b")
    c = _node("c")
    fts = [SearchHit(node=a, score=3.0, reason="fts"), SearchHit(node=b, score=2.0, reason="fts")]
    vec = [SearchHit(node=b, score=0.9, reason="vector"), SearchHit(node=c, score=0.8, reason="vector")]
    fused = _rrf_fuse([fts, vec], k=5, rrf_k=10)
    slugs = [h.node.slug for h in fused]
    # b appears in both lists at high ranks -> wins.
    assert slugs[0] == "b"
    # Reason reflects both sources.
    assert fused[0].reason == "fts+vector"


def test_rrf_handles_empty_lists():
    assert _rrf_fuse([], k=5, rrf_k=60) == []
    a = _node("a")
    fused = _rrf_fuse([[SearchHit(node=a, score=1.0, reason="fts")], []], k=5, rrf_k=60)
    assert [h.node.slug for h in fused] == ["a"]


# ---------------------------------------------------------------------------
# Capability-aware routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_markdown_backend_skips_vector_silently(tmp_path):
    backend = get_backend(f"file://{tmp_path}")
    await backend.init()
    try:
        await backend.put_node(_node("alpha", "signals"))
        retriever = HybridRetriever(backend, embedder=DeterministicEmbedder())
        hits = await retriever.retrieve("signals", k=5)
        # MarkdownBackend declares capabilities={"fts"}; vector is skipped.
        assert hits
        assert all("vector" not in h.reason for h in hits)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_null_embedder_disables_vector_path(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        node = _node("alpha", "signals quick brown fox")
        await backend.put_node(node)
        retriever = HybridRetriever(backend, embedder=NullEmbedder())
        hits = await retriever.retrieve("signals", k=5)
        assert hits
        # NullEmbedder has dimensions=0 -> vector disabled; only fts path ran.
        assert all(h.reason == "fts" for h in hits)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_embedder_failure_degrades_gracefully(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        node = _node("alpha", "signals alpha")
        await backend.put_node(node)
        retriever = HybridRetriever(backend, embedder=ExplodingEmbedder())
        hits = await retriever.retrieve("signals", k=5)
        # Vector retriever raised; fts still returned results.
        assert hits
        assert [h.node.slug for h in hits] == ["alpha"]
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# Vector + graph paths on SQLite backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_vector_search_finds_nearest(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        # Two notes with different content; we embed and store.
        a = _node("alpha", "quick brown fox")
        b = _node("beta", "completely unrelated")
        embedder = DeterministicEmbedder(dims=32)
        for node in (a, b):
            await backend.put_node(node)
            vec = await embedder.embed(node.body)
            await backend.put_embedding(node.id, vec, model="test-32d")
        query_vec = await embedder.embed("quick brown")
        hits = await backend.search_vector(query_vec, k=2)
        assert [h.node.slug for h in hits][0] == "alpha"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_graph_traverses_typed_links(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        a = _node("a")
        b = _node("b", links=[LinkRecord(src_slug="b", dst_slug="a", type="related")])
        c = _node("c", links=[LinkRecord(src_slug="c", dst_slug="b", type="related")])
        for n in (a, b, c):
            await backend.put_node(n)
        # Start at 'a'; 1-hop should see 'b' (inbound) but not 'c'.
        hits1 = await backend.search_graph("a", hops=1)
        assert {h.node.slug for h in hits1} == {"b"}
        # 2-hop should reach 'c' too.
        hits2 = await backend.search_graph("a", hops=2)
        assert {h.node.slug for h in hits2} == {"b", "c"}
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_hybrid_fuses_fts_and_vector_on_sqlite(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        embedder = DeterministicEmbedder(dims=48)
        for slug, body in (
            ("alpha", "quick brown fox"),
            ("beta", "quick lazy dog"),
            ("gamma", "completely different subject"),
        ):
            node = _node(slug, body)
            await backend.put_node(node)
            vec = await embedder.embed(body)
            await backend.put_embedding(node.id, vec)
        retriever = HybridRetriever(backend, embedder=embedder)
        hits = await retriever.retrieve("quick", k=3)
        slugs = [h.node.slug for h in hits]
        assert "alpha" in slugs and "beta" in slugs
        # At least one hit carries both retrievers in its reason field.
        assert any("+" in h.reason for h in hits)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_retriever_plan_can_disable_paths(tmp_path):
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        for slug, body in (("alpha", "quick"), ("beta", "lazy")):
            await backend.put_node(_node(slug, body))
        retriever = HybridRetriever(backend, embedder=DeterministicEmbedder())
        hits = await retriever.retrieve(
            "quick", k=3, plan=RetrievalPlan(fts=False, vector=True)
        )
        # Vector-only path returned nothing (no embeddings stored) but
        # must not raise; the retriever returns [].
        assert hits == []
    finally:
        await backend.close()
