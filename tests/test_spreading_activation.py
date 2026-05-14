"""Tests for memu.spreading_activation — bounded associative priming."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from memu.spreading_activation import (
    fetch_graph_neighbors,
    prime_agent_cache,
    activate_spreading,
    MAX_PRIMED_NEIGHBORS,
)
from memu.core_memory import Block, CoreMemoryManager


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

# Reuse the MockKV infrastructure from test_core_memory
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from test_core_memory import MockJetStream


@pytest.fixture
def mock_js():
    return MockJetStream()


@pytest.fixture
def core_mem(mock_js):
    return CoreMemoryManager(mock_js)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchGraphNeighbors:
    @pytest.mark.asyncio
    async def test_returns_empty_without_pool(self):
        result = await fetch_graph_neighbors("some-id", None)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_db_error(self):
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.fetch = AsyncMock(side_effect=Exception("DB error"))
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await fetch_graph_neighbors("some-id", mock_pool)
        assert result == []


class TestPrimeAgentCache:
    @pytest.mark.asyncio
    async def test_writes_to_primed_cache(self, core_mem):
        await core_mem.bootstrap_agent("lenny")
        neighbors = [
            {"name": "pgvector", "rel_type": "USES"},
            {"name": "port 5432", "rel_type": "LOCATED_IN"},
        ]
        success = await prime_agent_cache("lenny", neighbors, core_mem)
        assert success is True

        # Verify content was written to Primed_Cache
        entry = await core_mem.get_block("lenny", Block.PRIMED_CACHE)
        assert entry is not None
        assert "pgvector" in entry.content
        assert "port 5432" in entry.content

    @pytest.mark.asyncio
    async def test_respects_context_isolation(self, core_mem):
        """Primed_Cache is worker-writable, not agent-writable."""
        await core_mem.bootstrap_agent("lenny")
        neighbors = [{"name": "test", "rel_type": "RELATES_TO"}]
        # prime_agent_cache uses caller="worker" internally
        success = await prime_agent_cache("lenny", neighbors, core_mem)
        assert success is True

    @pytest.mark.asyncio
    async def test_returns_false_with_empty_neighbors(self, core_mem):
        success = await prime_agent_cache("lenny", [], core_mem)
        assert success is False

    @pytest.mark.asyncio
    async def test_returns_false_without_core_mem(self):
        success = await prime_agent_cache("lenny", [{"name": "x", "rel_type": "y"}], None)
        assert success is False


class TestActivateSpreading:
    @pytest.mark.asyncio
    async def test_returns_zero_without_neighbors(self, core_mem):
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        count = await activate_spreading("lenny", "some-id", mock_pool, core_mem)
        assert count == 0

    @pytest.mark.asyncio
    async def test_max_primed_neighbors_is_bounded(self):
        assert MAX_PRIMED_NEIGHBORS == 5

