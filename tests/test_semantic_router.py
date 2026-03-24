"""Tests for memu.semantic_router — intent-based query routing."""

from __future__ import annotations

import pytest
import numpy as np
from unittest.mock import AsyncMock

from memu.semantic_router import (
    SemanticRouter,
    SearchTarget,
    INTENT_ANCHORS,
    ROUTING_CONFIDENCE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIMS = 384


def make_embedding(seed: int = 42) -> list[float]:
    """Generate a deterministic fake embedding."""
    rng = np.random.RandomState(seed)
    vec = rng.randn(DIMS).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    return vec.tolist()


async def mock_get_embedding(text: str) -> list[float]:
    """Deterministic mock embedding based on text hash."""
    seed = hash(text) % (2**31)
    return make_embedding(seed)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSearchTarget:
    def test_all_targets_exist(self):
        assert SearchTarget.VECTOR
        assert SearchTarget.GRAPH
        assert SearchTarget.FTS
        assert SearchTarget.HYBRID


class TestIntentAnchors:
    def test_anchors_cover_all_targets(self):
        targets = {target for _, target in INTENT_ANCHORS}
        assert SearchTarget.VECTOR in targets
        assert SearchTarget.GRAPH in targets
        assert SearchTarget.FTS in targets

    def test_anchors_have_descriptions(self):
        for desc, target in INTENT_ANCHORS:
            assert len(desc) > 10, f"Anchor description too short: {desc}"


class TestSemanticRouter:
    @pytest.mark.asyncio
    async def test_not_initialized_returns_hybrid(self):
        router = SemanticRouter()
        assert not router.initialized
        assert router.route(make_embedding()) == SearchTarget.HYBRID

    @pytest.mark.asyncio
    async def test_initialize_succeeds(self):
        router = SemanticRouter()
        ok = await router.initialize(mock_get_embedding)
        assert ok is True
        assert router.initialized

    @pytest.mark.asyncio
    async def test_initialize_fails_with_no_embeddings(self):
        async def failing_embed(text: str):
            return None

        router = SemanticRouter()
        ok = await router.initialize(failing_embed)
        assert ok is False
        assert not router.initialized

    @pytest.mark.asyncio
    async def test_route_returns_valid_target(self):
        router = SemanticRouter()
        await router.initialize(mock_get_embedding)
        target = router.route(make_embedding(99))
        assert isinstance(target, SearchTarget)

    @pytest.mark.asyncio
    async def test_route_with_none_embedding_returns_hybrid(self):
        router = SemanticRouter()
        await router.initialize(mock_get_embedding)
        assert router.route(None) == SearchTarget.HYBRID

    @pytest.mark.asyncio
    async def test_route_with_zero_vector_returns_hybrid(self):
        router = SemanticRouter()
        await router.initialize(mock_get_embedding)
        assert router.route([0.0] * DIMS) == SearchTarget.HYBRID

    @pytest.mark.asyncio
    async def test_route_latency_under_50ms(self):
        """Routing must be < 50ms (no LLM in the hot path)."""
        import time
        router = SemanticRouter()
        await router.initialize(mock_get_embedding)

        emb = make_embedding(123)
        start = time.monotonic()
        for _ in range(100):
            router.route(emb)
        elapsed_ms = (time.monotonic() - start) * 1000

        avg_ms = elapsed_ms / 100
        assert avg_ms < 50, f"Average routing latency {avg_ms:.2f}ms exceeds 50ms"

    @pytest.mark.asyncio
    async def test_similar_query_routes_consistently(self):
        """Same embedding should always route to the same target."""
        router = SemanticRouter()
        await router.initialize(mock_get_embedding)

        emb = make_embedding(42)
        results = [router.route(emb) for _ in range(10)]
        assert len(set(results)) == 1, "Routing is not deterministic"

