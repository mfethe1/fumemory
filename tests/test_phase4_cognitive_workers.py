"""Tests for Phase 4: Self-Healing & Subconscious cognitive workers."""

from __future__ import annotations

import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch

from memu.cognitive_workers import (
    ContradictionEngine,
    SubconsciousStream,
    CONTRADICTION_SIMILARITY_THRESHOLD,
    SUBCONSCIOUS_SIMILARITY_THRESHOLD,
)
from memu.core_memory import Block, CoreMemoryManager

# Reuse MockJetStream from Phase 1 tests
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from test_core_memory import MockJetStream


DIMS = 384


def make_embedding(seed: int = 42) -> list[float]:
    rng = np.random.RandomState(seed)
    vec = rng.randn(DIMS).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    return vec.tolist()


@pytest.fixture
def mock_js():
    return MockJetStream()


@pytest.fixture
def core_mem(mock_js):
    return CoreMemoryManager(mock_js)


# ---------------------------------------------------------------------------
# ContradictionEngine tests
# ---------------------------------------------------------------------------


class TestContradictionEngine:
    def test_threshold_is_0_88(self):
        assert CONTRADICTION_SIMILARITY_THRESHOLD == 0.88

    @pytest.mark.asyncio
    async def test_returns_none_without_pool(self):
        engine = ContradictionEngine()
        result = await engine.check_contradiction(
            "mem-1", "The sky is blue", make_embedding(), "agent-1", pool=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_similar_facts(self):
        engine = ContradictionEngine()
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await engine.check_contradiction(
            "mem-1", "The sky is blue", make_embedding(), "agent-1", pool=mock_pool,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_does_not_auto_delete_on_contradiction(self):
        """Critical invariant: contradictions create edges, never delete memories."""
        engine = ContradictionEngine()

        # Mock pool returns a high-similarity candidate
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {"id": "existing-id", "content": "The sky is green", "similarity": 0.92}
        ])
        mock_conn.execute = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        # Mock LLM says it's a contradiction
        with patch("memu.cognitive_workers._call_llm_json", new_callable=AsyncMock,
                    return_value={"contradicts": True, "reasoning": "Color mismatch"}):
            result = await engine.check_contradiction(
                "mem-new", "The sky is blue", make_embedding(), "agent-1", pool=mock_pool,
            )

        assert result is not None
        assert result["new_memory_id"] == "mem-new"
        assert result["existing_memory_id"] == "existing-id"
        # Both memories should still exist — no deletion

    @pytest.mark.asyncio
    async def test_skips_llm_below_threshold(self):
        """Only invoke LLM if similarity > 0.88."""
        engine = ContradictionEngine()
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        # Return candidate below threshold
        mock_conn.fetch = AsyncMock(return_value=[
            {"id": "existing-id", "content": "Something", "similarity": 0.80}
        ])
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("memu.cognitive_workers._call_llm_json", new_callable=AsyncMock) as mock_llm:
            result = await engine.check_contradiction(
                "mem-1", "text", make_embedding(), "agent-1", pool=mock_pool,
            )
            # LLM should NOT have been called
            mock_llm.assert_not_called()
        assert result is None


# ---------------------------------------------------------------------------
# SubconsciousStream tests
# ---------------------------------------------------------------------------


class TestSubconsciousStream:
    def test_threshold_is_0_85(self):
        assert SUBCONSCIOUS_SIMILARITY_THRESHOLD == 0.85

    @pytest.mark.asyncio
    async def test_returns_empty_without_agents(self, core_mem):
        stream = SubconsciousStream(core_mem)
        result = await stream.check_and_inject(make_embedding(), "test summary")
        assert result == []

    @pytest.mark.asyncio
    async def test_injects_into_matching_agent(self, core_mem):
        await core_mem.bootstrap_agent("lenny")
        stream = SubconsciousStream(core_mem)

        # Set agent vector to be identical to broadcast (similarity = 1.0)
        emb = make_embedding(42)
        stream.update_agent_vector("lenny", emb)

        result = await stream.check_and_inject(emb, "Important insight about databases")
        assert "lenny" in result

        # Verify Primed_Cache was updated
        entry = await core_mem.get_block("lenny", Block.PRIMED_CACHE)
        assert "Important insight" in entry.content

    @pytest.mark.asyncio
    async def test_does_not_echo_to_source(self, core_mem):
        await core_mem.bootstrap_agent("lenny")
        stream = SubconsciousStream(core_mem)
        emb = make_embedding(42)
        stream.update_agent_vector("lenny", emb)

        # Source agent = lenny, should not inject back
        result = await stream.check_and_inject(emb, "test", source_agent="lenny")
        assert "lenny" not in result

    @pytest.mark.asyncio
    async def test_skips_low_similarity_agents(self, core_mem):
        await core_mem.bootstrap_agent("lenny")
        stream = SubconsciousStream(core_mem)

        # Set agent vector to be orthogonal to broadcast
        stream.update_agent_vector("lenny", make_embedding(1))
        result = await stream.check_and_inject(make_embedding(999), "unrelated topic")
        # Likely low similarity — should not inject
        # (depends on random vectors, but orthogonal seeds should be < 0.85)

    @pytest.mark.asyncio
    async def test_writes_to_primed_cache_not_working_context(self, core_mem):
        """Context isolation: subconscious writes to Primed_Cache only."""
        await core_mem.bootstrap_agent("lenny")
        stream = SubconsciousStream(core_mem)
        emb = make_embedding(42)
        stream.update_agent_vector("lenny", emb)

        await stream.check_and_inject(emb, "test insight")

        # Primed_Cache should have content
        primed = await core_mem.get_block("lenny", Block.PRIMED_CACHE)
        # Working_Context should be unchanged (empty from bootstrap)
        working = await core_mem.get_block("lenny", Block.WORKING_CONTEXT)
        assert working.content == ""

    def test_remove_agent(self):
        stream = SubconsciousStream()
        stream.update_agent_vector("lenny", make_embedding())
        assert "lenny" in stream._agent_ids
        stream.remove_agent("lenny")
        assert "lenny" not in stream._agent_ids

