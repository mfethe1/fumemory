"""Tests for memu.core_memory — CAS concurrency, token limits, permission enforcement.

These tests use a mock NATS KV store to verify logic without a running NATS server.
"""

from __future__ import annotations

import pytest
import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from memu.core_memory import (
    Block,
    BlockEntry,
    AgentCoreMemory,
    CoreMemoryManager,
    CASConflictError,
    count_tokens,
    AGENT_WRITABLE_BLOCKS,
    WORKER_WRITABLE_BLOCKS,
    DEFAULT_TOKEN_LIMITS,
    TOTAL_TOKEN_BUDGET,
    MAX_CAS_RETRIES,
)


# ---------------------------------------------------------------------------
# Mock NATS KV
# ---------------------------------------------------------------------------

class MockKVEntry:
    """Simulates a nats.js.kv.KeyValueEntry."""
    def __init__(self, key: str, value: bytes, revision: int):
        self.key = key
        self.value = value
        self.revision = revision


class MockKeyValue:
    """In-memory mock of nats.js.kv.KeyValue for testing CAS logic."""

    def __init__(self):
        self._store: dict[str, MockKVEntry] = {}
        self._next_rev: int = 1

    async def get(self, key: str) -> MockKVEntry:
        if key not in self._store:
            from nats.js.errors import KeyNotFoundError
            raise KeyNotFoundError()
        return self._store[key]

    async def create(self, key: str, value: bytes) -> int:
        if key in self._store:
            raise Exception("key already in use")
        rev = self._next_rev
        self._next_rev += 1
        self._store[key] = MockKVEntry(key, value, rev)
        return rev

    async def update(self, key: str, value: bytes, last: int) -> int:
        if key not in self._store:
            from nats.errors import KeyNotFoundError
            raise KeyNotFoundError()
        if self._store[key].revision != last:
            raise Exception("nats: wrong last sequence")
        rev = self._next_rev
        self._next_rev += 1
        self._store[key] = MockKVEntry(key, value, rev)
        return rev

    async def purge(self, key: str):
        if key in self._store:
            del self._store[key]


class MockJetStream:
    """Mock JetStreamContext that returns MockKeyValue."""

    def __init__(self):
        self._kv = MockKeyValue()
        self._created = False

    async def key_value(self, bucket: str):
        if not self._created:
            raise Exception("bucket not found")
        return self._kv

    async def create_key_value(self, **kwargs):
        self._created = True
        return self._kv


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_js():
    return MockJetStream()


@pytest.fixture
def mgr(mock_js):
    return CoreMemoryManager(mock_js)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestCountTokens:
    def test_basic_counting(self):
        tokens = count_tokens("Hello world")
        assert tokens >= 1

    def test_empty_string(self):
        assert count_tokens("") >= 1 or count_tokens("") == 0

    def test_long_text(self):
        text = "word " * 1000
        tokens = count_tokens(text)
        assert tokens > 100


class TestBlockEntry:
    def test_to_dict(self):
        entry = BlockEntry(content="test", revision=5, tokens=1)
        d = entry.to_dict()
        assert d["content"] == "test"
        assert d["revision"] == 5
        assert d["tokens"] == 1


class TestAgentCoreMemory:
    def test_total_tokens(self):
        mem = AgentCoreMemory(agent_id="test")
        mem.blocks["a"] = BlockEntry(content="x", revision=1, tokens=10)
        mem.blocks["b"] = BlockEntry(content="y", revision=1, tokens=20)
        assert mem.total_tokens == 30

    def test_to_dict(self):
        mem = AgentCoreMemory(agent_id="test")
        d = mem.to_dict()
        assert d["agent_id"] == "test"
        assert d["budget"] == TOTAL_TOKEN_BUDGET



# ---------------------------------------------------------------------------
# CoreMemoryManager tests
# ---------------------------------------------------------------------------

class TestEnsureBucket:
    @pytest.mark.asyncio
    async def test_creates_bucket_on_first_call(self, mgr, mock_js):
        kv = await mgr.ensure_bucket()
        assert kv is not None
        assert mock_js._created is True

    @pytest.mark.asyncio
    async def test_idempotent_on_second_call(self, mgr, mock_js):
        kv1 = await mgr.ensure_bucket()
        mock_js._created = True  # simulate existing
        kv2 = await mgr.ensure_bucket()
        assert kv1 is kv2


class TestBootstrapAgent:
    @pytest.mark.asyncio
    async def test_bootstrap_creates_all_blocks(self, mgr):
        mem = await mgr.bootstrap_agent("lenny", system="You are helpful", persona="Lenny the coder")
        assert len(mem.blocks) == len(Block)
        assert mem.blocks[Block.SYSTEM.value].content == "You are helpful"
        assert mem.blocks[Block.PERSONA.value].content == "Lenny the coder"
        assert mem.blocks[Block.WORKING_CONTEXT.value].content == ""

    @pytest.mark.asyncio
    async def test_bootstrap_is_idempotent(self, mgr):
        await mgr.bootstrap_agent("lenny", system="v1")
        mem2 = await mgr.bootstrap_agent("lenny", system="v2")
        # Should NOT overwrite existing System block
        assert mem2.blocks[Block.SYSTEM.value].content == "v1"


class TestGetBlock:
    @pytest.mark.asyncio
    async def test_returns_none_for_missing(self, mgr):
        await mgr.ensure_bucket()
        entry = await mgr.get_block("ghost", Block.SYSTEM)
        assert entry is None

    @pytest.mark.asyncio
    async def test_returns_entry_after_bootstrap(self, mgr):
        await mgr.bootstrap_agent("lenny", system="test sys")
        entry = await mgr.get_block("lenny", Block.SYSTEM)
        assert entry is not None
        assert entry.content == "test sys"
        assert entry.revision > 0


class TestGetAgentMemory:
    @pytest.mark.asyncio
    async def test_returns_all_blocks(self, mgr):
        await mgr.bootstrap_agent("lenny")
        mem = await mgr.get_agent_memory("lenny")
        assert len(mem.blocks) == len(Block)

    @pytest.mark.asyncio
    async def test_missing_agent_returns_empty_blocks(self, mgr):
        await mgr.ensure_bucket()
        mem = await mgr.get_agent_memory("nobody")
        for block in Block:
            assert mem.blocks[block.value].content == ""
            assert mem.blocks[block.value].revision == 0


class TestUpdateBlock:
    @pytest.mark.asyncio
    async def test_agent_can_write_working_context(self, mgr):
        await mgr.bootstrap_agent("lenny")
        mem = await mgr.get_agent_memory("lenny")
        rev = mem.blocks[Block.WORKING_CONTEXT.value].revision
        entry = await mgr.update_block("lenny", Block.WORKING_CONTEXT, "new context", rev, caller="agent")
        assert entry.content == "new context"
        assert entry.revision > rev

    @pytest.mark.asyncio
    async def test_agent_cannot_write_system(self, mgr):
        await mgr.bootstrap_agent("lenny")
        with pytest.raises(PermissionError):
            await mgr.update_block("lenny", Block.SYSTEM, "hacked", 1, caller="agent")

    @pytest.mark.asyncio
    async def test_agent_cannot_write_primed_cache(self, mgr):
        await mgr.bootstrap_agent("lenny")
        with pytest.raises(PermissionError):
            await mgr.update_block("lenny", Block.PRIMED_CACHE, "data", 1, caller="agent")

    @pytest.mark.asyncio
    async def test_agent_cannot_write_user_profile(self, mgr):
        await mgr.bootstrap_agent("lenny")
        with pytest.raises(PermissionError):
            await mgr.update_block("lenny", Block.USER_PROFILE, "data", 1, caller="agent")

    @pytest.mark.asyncio
    async def test_worker_can_write_user_profile(self, mgr):
        await mgr.bootstrap_agent("lenny")
        mem = await mgr.get_agent_memory("lenny")
        rev = mem.blocks[Block.USER_PROFILE.value].revision
        entry = await mgr.update_block("lenny", Block.USER_PROFILE, '{"pref": "dark"}', rev, caller="worker")
        assert entry.content == '{"pref": "dark"}'

    @pytest.mark.asyncio
    async def test_worker_can_write_primed_cache(self, mgr):
        await mgr.bootstrap_agent("lenny")
        mem = await mgr.get_agent_memory("lenny")
        rev = mem.blocks[Block.PRIMED_CACHE.value].revision
        entry = await mgr.update_block("lenny", Block.PRIMED_CACHE, "primed data", rev, caller="worker")
        assert entry.content == "primed data"

    @pytest.mark.asyncio
    async def test_worker_cannot_write_working_context(self, mgr):
        await mgr.bootstrap_agent("lenny")
        with pytest.raises(PermissionError):
            await mgr.update_block("lenny", Block.WORKING_CONTEXT, "hack", 1, caller="worker")

    @pytest.mark.asyncio
    async def test_token_budget_enforcement(self, mgr):
        await mgr.bootstrap_agent("lenny")
        huge_content = "word " * 50_000  # Way over any limit
        with pytest.raises(ValueError, match="exceeding"):
            await mgr.update_block("lenny", Block.WORKING_CONTEXT, huge_content, 0, caller="agent")

    @pytest.mark.asyncio
    async def test_cas_conflict_recovery(self, mgr):
        """Simulate a CAS conflict where another writer updates between read and write."""
        await mgr.bootstrap_agent("lenny")
        mem = await mgr.get_agent_memory("lenny")
        stale_rev = mem.blocks[Block.WORKING_CONTEXT.value].revision

        # Simulate another writer updating the block
        kv = await mgr.ensure_bucket()
        key = mgr._key("lenny", Block.WORKING_CONTEXT)
        await kv.update(key, b"concurrent write", stale_rev)

        # Now try to write with stale revision — should auto-recover
        entry = await mgr.update_block("lenny", Block.WORKING_CONTEXT, "my write", stale_rev, caller="agent")
        assert entry.content == "my write"


class TestAppendBlock:
    @pytest.mark.asyncio
    async def test_append_to_existing(self, mgr):
        await mgr.bootstrap_agent("lenny")
        mem = await mgr.get_agent_memory("lenny")
        rev = mem.blocks[Block.WORKING_CONTEXT.value].revision
        await mgr.update_block("lenny", Block.WORKING_CONTEXT, "first", rev, caller="agent")
        entry = await mgr.append_block("lenny", Block.WORKING_CONTEXT, "second", caller="agent")
        assert "first" in entry.content
        assert "second" in entry.content

    @pytest.mark.asyncio
    async def test_append_to_empty_creates(self, mgr):
        await mgr.ensure_bucket()
        entry = await mgr.append_block("newagent", Block.WORKING_CONTEXT, "hello", caller="agent")
        assert entry.content == "hello"

    @pytest.mark.asyncio
    async def test_append_rejects_over_budget(self, mgr):
        await mgr.bootstrap_agent("lenny")
        huge = "word " * 50_000
        with pytest.raises(ValueError, match="exceeding"):
            await mgr.append_block("lenny", Block.WORKING_CONTEXT, huge, caller="agent")


class TestReplaceInBlock:
    @pytest.mark.asyncio
    async def test_replace_text(self, mgr):
        await mgr.bootstrap_agent("lenny")
        mem = await mgr.get_agent_memory("lenny")
        rev = mem.blocks[Block.WORKING_CONTEXT.value].revision
        await mgr.update_block("lenny", Block.WORKING_CONTEXT, "hello world foo", rev, caller="agent")
        entry = await mgr.replace_in_block("lenny", Block.WORKING_CONTEXT, "foo", "bar", caller="agent")
        assert entry.content == "hello world bar"

    @pytest.mark.asyncio
    async def test_replace_missing_text_raises(self, mgr):
        await mgr.bootstrap_agent("lenny")
        mem = await mgr.get_agent_memory("lenny")
        rev = mem.blocks[Block.WORKING_CONTEXT.value].revision
        await mgr.update_block("lenny", Block.WORKING_CONTEXT, "hello world", rev, caller="agent")
        with pytest.raises(ValueError, match="old_text not found"):
            await mgr.replace_in_block("lenny", Block.WORKING_CONTEXT, "xyz", "abc", caller="agent")


class TestDeleteAgent:
    @pytest.mark.asyncio
    async def test_delete_purges_all_blocks(self, mgr):
        await mgr.bootstrap_agent("lenny")
        deleted = await mgr.delete_agent("lenny")
        assert deleted == len(Block)
        # Verify blocks are gone
        mem = await mgr.get_agent_memory("lenny")
        for block in Block:
            assert mem.blocks[block.value].content == ""


class TestContextIsolation:
    """Verify the critical invariant: agents and workers have separate write domains."""

    def test_agent_writable_blocks_are_correct(self):
        assert Block.WORKING_CONTEXT in AGENT_WRITABLE_BLOCKS
        assert Block.PRIMED_CACHE not in AGENT_WRITABLE_BLOCKS
        assert Block.USER_PROFILE not in AGENT_WRITABLE_BLOCKS
        assert Block.SYSTEM not in AGENT_WRITABLE_BLOCKS

    def test_worker_writable_blocks_are_correct(self):
        assert Block.PRIMED_CACHE in WORKER_WRITABLE_BLOCKS
        assert Block.USER_PROFILE in WORKER_WRITABLE_BLOCKS
        assert Block.WORKING_CONTEXT not in WORKER_WRITABLE_BLOCKS
        assert Block.SYSTEM not in WORKER_WRITABLE_BLOCKS

    def test_no_overlap_between_agent_and_worker(self):
        overlap = AGENT_WRITABLE_BLOCKS & WORKER_WRITABLE_BLOCKS
        assert len(overlap) == 0, f"Overlap detected: {overlap}"

