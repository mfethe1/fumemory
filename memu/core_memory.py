"""Core Memory Manager — NATS KV-backed agent working memory (Letta-parity).

Treats the LLM context window like RAM. Each agent gets isolated KV blocks:
  - System:          Immutable system prompt fragment (set once by orchestrator)
  - Persona:         Agent personality / role description
  - User_Profile:    Autonomously updated by the Implicit Profiling Daemon
  - Working_Context: Agent-writable scratch pad (tool calls, reasoning state)
  - Primed_Cache:    Read-only for agent; written by background workers (spreading activation)

ALL writes use NATS Compare-And-Swap (CAS) via revision tracking to prevent
race conditions between agents and background workers.

Token counting uses tiktoken to enforce strict size boundaries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from nats.js.kv import KeyValue
from nats.js import JetStreamContext
from nats.js.errors import KeyNotFoundError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET_NAME = "memu_core_memory"
BUCKET_TTL_SECONDS = 0  # 0 = no expiry
BUCKET_HISTORY = 5  # keep last 5 revisions for rollback
MAX_VALUE_BYTES = 128 * 1024  # 128 KiB hard cap per KV entry


class Block(str, Enum):
    """Memory block names — strict enum prevents typo-induced fragmentation."""
    SYSTEM = "System"
    PERSONA = "Persona"
    USER_PROFILE = "User_Profile"
    WORKING_CONTEXT = "Working_Context"
    PRIMED_CACHE = "Primed_Cache"


# Blocks that agents may write to directly via tools.
AGENT_WRITABLE_BLOCKS = frozenset({Block.WORKING_CONTEXT})

# Blocks that background workers may write to.
WORKER_WRITABLE_BLOCKS = frozenset({Block.PRIMED_CACHE, Block.USER_PROFILE})

# Default token limits per block (cl100k_base tokens).
DEFAULT_TOKEN_LIMITS: dict[Block, int] = {
    Block.SYSTEM: 1_500,
    Block.PERSONA: 1_000,
    Block.USER_PROFILE: 2_000,
    Block.WORKING_CONTEXT: 4_000,
    Block.PRIMED_CACHE: 2_000,
}

# Total context budget across all blocks.
TOTAL_TOKEN_BUDGET = 12_000


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

_tokenizer: Any = None


def _get_tokenizer():
    """Lazy-load tiktoken (cl100k_base) — falls back to rough estimator."""
    global _tokenizer
    if _tokenizer is not None:
        return _tokenizer
    try:
        import tiktoken
        _tokenizer = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _tokenizer = None
    return _tokenizer


def count_tokens(text: str) -> int:
    """Count tokens in *text*. Uses tiktoken if available, else ~4 chars/token."""
    enc = _get_tokenizer()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // 4)


def token_safe_truncate(
    data: list | dict,
    max_tokens: int,
    protected_keys: set[str] | None = None,
) -> list | dict:
    """Truncate a list or dict by removing the oldest items until the
    JSON serialization fits within *max_tokens*.

    This avoids raw string slicing which would corrupt JSON structure.
    Items are popped from the front of lists (oldest first) or the
    first-inserted key of dicts (respecting ``protected_keys``).
    Returns a *new* (shallow-copied) container — the original is not mutated.

    Args:
        data: The list or dict to truncate.
        max_tokens: Target token budget.
        protected_keys: (dicts only) Keys that must never be evicted.
            If all non-protected keys are exhausted and the dict still
            exceeds *max_tokens*, a warning is logged and the dict is
            returned as-is.

    If a single remaining item still exceeds the budget, it is kept to avoid
    returning an empty container (caller should handle oversized singletons).
    """
    if isinstance(data, list):
        data = list(data)  # shallow copy
        while len(data) > 1 and count_tokens(json.dumps(data)) > max_tokens:
            data.pop(0)  # remove oldest entry
    elif isinstance(data, dict):
        data = dict(data)  # shallow copy
        protected = protected_keys or set()
        # Build list of evictable keys (insertion order, skip protected)
        evictable = [k for k in data.keys() if k not in protected]
        idx = 0
        while len(data) > 1 and count_tokens(json.dumps(data)) > max_tokens:
            if idx < len(evictable):
                del data[evictable[idx]]
                idx += 1
            else:
                # All non-protected keys exhausted — cannot shrink further
                logger.warning(
                    "token_safe_truncate: dict still exceeds %d tokens after "
                    "evicting all non-protected keys (protected=%s)",
                    max_tokens, protected,
                )
                break
    return data


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BlockEntry:
    """Single block snapshot returned from NATS KV."""
    content: str
    revision: int  # CAS revision for safe updates
    tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "revision": self.revision, "tokens": self.tokens}


@dataclass
class AgentCoreMemory:
    """Full core-memory snapshot for one agent."""
    agent_id: str
    blocks: dict[str, BlockEntry] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return sum(b.tokens for b in self.blocks.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "total_tokens": self.total_tokens,
            "budget": TOTAL_TOKEN_BUDGET,
            "blocks": {k: v.to_dict() for k, v in self.blocks.items()},
        }

    def as_system_prompt_fragment(self) -> str:
        """Render all blocks into a single string for LLM system-prompt injection."""
        parts: list[str] = []
        for block_name in Block:
            entry = self.blocks.get(block_name.value)
            if entry and entry.content:
                parts.append(f"<{block_name.value}>\n{entry.content}\n</{block_name.value}>")
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CAS retry logic
# ---------------------------------------------------------------------------

class MemoryConcurrencyError(Exception):
    """Raised when CAS retry budget is exhausted after exponential backoff."""


# Legacy alias for backward compatibility
CASConflictError = MemoryConcurrencyError

MAX_CAS_RETRIES = 5
CAS_BASE_DELAY = 0.1  # 100ms base delay for exponential backoff


# ---------------------------------------------------------------------------
# Core Memory Manager
# ---------------------------------------------------------------------------

class CoreMemoryManager:
    """Manages per-agent NATS KV core memory with CAS concurrency control.

    Usage::

        mgr = CoreMemoryManager(js)          # js = JetStreamContext
        await mgr.ensure_bucket()             # idempotent bucket creation
        mem = await mgr.get_agent_memory("agent-lenny")
        await mgr.update_block("agent-lenny", Block.WORKING_CONTEXT, "new text", revision=mem.blocks["Working_Context"].revision)
    """

    def __init__(self, js: JetStreamContext):
        self._js = js
        self._kv: KeyValue | None = None

    async def ensure_bucket(self) -> KeyValue:
        """Create or open the core-memory KV bucket (idempotent)."""
        if self._kv is not None:
            return self._kv
        try:
            self._kv = await self._js.key_value(BUCKET_NAME)
            logger.info("Opened existing NATS KV bucket: %s", BUCKET_NAME)
        except Exception:
            self._kv = await self._js.create_key_value(
                bucket=BUCKET_NAME,
                description="memU agent core memory blocks",
                history=BUCKET_HISTORY,
                max_value_size=MAX_VALUE_BYTES,
            )
            logger.info("Created NATS KV bucket: %s", BUCKET_NAME)
        return self._kv

    def _key(self, agent_id: str, block: Block | str) -> str:
        """Build the KV key: ``agent_{id}.{Block}``."""
        block_name = block.value if isinstance(block, Block) else block
        return f"agent_{agent_id}.{block_name}"

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_block(self, agent_id: str, block: Block) -> BlockEntry | None:
        """Fetch a single block. Returns ``None`` if the key does not exist."""
        kv = await self.ensure_bucket()
        try:
            entry = await kv.get(self._key(agent_id, block))
            content = entry.value.decode() if entry.value else ""
            return BlockEntry(content=content, revision=entry.revision, tokens=count_tokens(content))
        except KeyNotFoundError:
            return None
        except Exception as exc:
            logger.warning("Failed to read %s/%s: %s", agent_id, block.value, exc)
            return None

    async def get_agent_memory(self, agent_id: str) -> AgentCoreMemory:
        """Fetch all blocks for an agent. Missing blocks come back empty."""
        mem = AgentCoreMemory(agent_id=agent_id)
        for block in Block:
            entry = await self.get_block(agent_id, block)
            if entry is not None:
                mem.blocks[block.value] = entry
            else:
                mem.blocks[block.value] = BlockEntry(content="", revision=0, tokens=0)
        return mem

    # ------------------------------------------------------------------
    # Write operations (CAS-protected)
    # ------------------------------------------------------------------

    async def update_block(
        self,
        agent_id: str,
        block: Block,
        content: str,
        expected_revision: int = 0,
        *,
        caller: str = "agent",
    ) -> BlockEntry:
        """Write *content* to a block using CAS.

        Args:
            agent_id: Target agent.
            block: Which block to write.
            content: Full replacement content.
            expected_revision: Last known revision (0 = create-if-absent).
            caller: ``"agent"`` or ``"worker"`` — enforces write-permission rules.

        Raises:
            PermissionError: If caller violates write-access rules.
            CASConflictError: If CAS fails after MAX_CAS_RETRIES.
            ValueError: If content exceeds token budget or is quarantined.
        """
        # Cognitive Firewall — sanitize all writes against prompt injection
        try:
            from memu.memory_agent import sanitize_memory_payload
            is_safe, matched = sanitize_memory_payload(content)
            if not is_safe:
                logger.warning(
                    "Cognitive Firewall BLOCKED write to %s/%s — "
                    "quarantined pattern: '%s'",
                    agent_id, block.value, matched,
                )
                raise ValueError(
                    f"Content quarantined: prompt injection detected (pattern: {matched})"
                )
        except ImportError:
            pass  # memory_agent not available — skip firewall

        # Permission enforcement
        if caller == "agent" and block not in AGENT_WRITABLE_BLOCKS:
            raise PermissionError(
                f"Agents may only write to {[b.value for b in AGENT_WRITABLE_BLOCKS]}; "
                f"got {block.value}"
            )
        if caller == "worker" and block not in WORKER_WRITABLE_BLOCKS:
            raise PermissionError(
                f"Workers may only write to {[b.value for b in WORKER_WRITABLE_BLOCKS]}; "
                f"got {block.value}"
            )

        # Token budget enforcement
        tokens = count_tokens(content)
        limit = DEFAULT_TOKEN_LIMITS.get(block, 4_000)
        if tokens > limit:
            raise ValueError(
                f"Content has {tokens} tokens, exceeding {block.value} limit of {limit}"
            )

        kv = await self.ensure_bucket()
        key = self._key(agent_id, block)
        data = content.encode()

        # Full replacement — content is caller-supplied, only revision needs refresh
        revision = expected_revision
        for attempt in range(1, MAX_CAS_RETRIES + 1):
            try:
                if revision == 0:
                    # First write — create key
                    new_rev = await kv.create(key, data)
                else:
                    new_rev = await kv.update(key, data, revision)

                logger.debug(
                    "CAS write OK: %s rev %d→%d (attempt %d)",
                    key, revision, new_rev, attempt,
                )
                return BlockEntry(content=content, revision=new_rev, tokens=tokens)

            except Exception as exc:
                err_str = str(exc).lower()
                is_cas_conflict = (
                    "wrong last sequence" in err_str
                    or "nats: wrong last sequence" in err_str
                )
                is_key_exists = "already in use" in err_str

                if is_cas_conflict or is_key_exists:
                    # Fetch latest revision and retry with exponential backoff + jitter
                    delay = CAS_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, CAS_BASE_DELAY)
                    logger.info(
                        "CAS conflict on %s (attempt %d/%d), backing off %.3fs",
                        key, attempt, MAX_CAS_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    try:
                        latest = await kv.get(key)
                        revision = latest.revision
                    except KeyNotFoundError:
                        revision = 0
                else:
                    raise

        raise MemoryConcurrencyError(
            f"CAS failed after {MAX_CAS_RETRIES} retries on {key}"
        )

    async def append_block(
        self,
        agent_id: str,
        block: Block,
        text: str,
        expected_revision: int = 0,
        *,
        caller: str = "agent",
        separator: str = "\n",
    ) -> BlockEntry:
        """Append *text* to an existing block (read-modify-write with CAS)."""
        kv = await self.ensure_bucket()
        key = self._key(agent_id, block)

        revision = expected_revision
        for attempt in range(1, MAX_CAS_RETRIES + 1):
            # Read current
            try:
                entry = await kv.get(key)
                current = entry.value.decode() if entry.value else ""
                revision = entry.revision
            except KeyNotFoundError:
                current = ""
                revision = 0

            new_content = (current + separator + text).strip() if current else text.strip()

            # Token check
            tokens = count_tokens(new_content)
            limit = DEFAULT_TOKEN_LIMITS.get(block, 4_000)
            if tokens > limit:
                raise ValueError(
                    f"Appended content would be {tokens} tokens, exceeding {block.value} limit of {limit}"
                )

            try:
                if revision == 0:
                    new_rev = await kv.create(key, new_content.encode())
                else:
                    new_rev = await kv.update(key, new_content.encode(), revision)
                return BlockEntry(content=new_content, revision=new_rev, tokens=tokens)
            except Exception as exc:
                err_str = str(exc).lower()
                if "wrong last sequence" in err_str or "already in use" in err_str:
                    delay = CAS_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, CAS_BASE_DELAY)
                    logger.info(
                        "CAS conflict in append on %s (attempt %d/%d), backing off %.3fs",
                        key, attempt, MAX_CAS_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

        raise MemoryConcurrencyError(f"CAS append failed after {MAX_CAS_RETRIES} retries on {key}")

    async def replace_in_block(
        self,
        agent_id: str,
        block: Block,
        old_text: str,
        new_text: str,
        expected_revision: int = 0,
        *,
        caller: str = "agent",
    ) -> BlockEntry:
        """Replace *old_text* with *new_text* inside a block (CAS-protected).

        Inlines CAS write logic to avoid nested retry loops (replace_in_block
        previously delegated to update_block which has its own retry loop,
        creating up to 5×5 = 25 attempts). Now: single flat retry loop that
        re-reads latest state on each conflict and re-applies the replacement.
        """
        # Permission enforcement
        if caller == "agent" and block not in AGENT_WRITABLE_BLOCKS:
            raise PermissionError(
                f"Agents may only write to {[b.value for b in AGENT_WRITABLE_BLOCKS]}; "
                f"got {block.value}"
            )
        if caller == "worker" and block not in WORKER_WRITABLE_BLOCKS:
            raise PermissionError(
                f"Workers may only write to {[b.value for b in WORKER_WRITABLE_BLOCKS]}; "
                f"got {block.value}"
            )

        kv = await self.ensure_bucket()
        key = self._key(agent_id, block)

        for attempt in range(1, MAX_CAS_RETRIES + 1):
            # (1) Fetch latest content + revision
            try:
                entry = await kv.get(key)
                current = entry.value.decode() if entry.value else ""
                revision = entry.revision
            except KeyNotFoundError:
                raise ValueError(f"Block {block.value} does not exist for agent {agent_id}")

            # (2) Verify old_text exists in current content
            if old_text not in current:
                raise ValueError(
                    f"old_text not found in {block.value} for agent {agent_id}"
                )

            # (3) Compute new content from latest state
            new_content = current.replace(old_text, new_text, 1)

            # Token budget enforcement
            tokens = count_tokens(new_content)
            limit = DEFAULT_TOKEN_LIMITS.get(block, 4_000)
            if tokens > limit:
                raise ValueError(
                    f"Replaced content has {tokens} tokens, exceeding {block.value} limit of {limit}"
                )

            # (4) Attempt CAS update
            try:
                new_rev = await kv.update(key, new_content.encode(), revision)
                logger.debug(
                    "CAS replace OK: %s rev %d→%d (attempt %d)",
                    key, revision, new_rev, attempt,
                )
                return BlockEntry(content=new_content, revision=new_rev, tokens=tokens)
            except Exception as exc:
                err_str = str(exc).lower()
                if "wrong last sequence" in err_str or "already in use" in err_str:
                    delay = CAS_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, CAS_BASE_DELAY)
                    logger.info(
                        "CAS conflict in replace on %s (attempt %d/%d), backing off %.3fs",
                        key, attempt, MAX_CAS_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

        raise MemoryConcurrencyError(f"CAS replace failed after {MAX_CAS_RETRIES} retries on {key}")

    # ------------------------------------------------------------------
    # Admin / bootstrap operations (no permission checks)
    # ------------------------------------------------------------------

    async def bootstrap_agent(
        self,
        agent_id: str,
        system: str = "",
        persona: str = "",
    ) -> AgentCoreMemory:
        """Initialize all blocks for a new agent. Idempotent — skips existing blocks."""
        defaults = {
            Block.SYSTEM: system,
            Block.PERSONA: persona,
            Block.USER_PROFILE: "",
            Block.WORKING_CONTEXT: "",
            Block.PRIMED_CACHE: "",
        }
        kv = await self.ensure_bucket()
        mem = AgentCoreMemory(agent_id=agent_id)

        for block, default_content in defaults.items():
            key = self._key(agent_id, block)
            try:
                entry = await kv.get(key)
                content = entry.value.decode() if entry.value else ""
                mem.blocks[block.value] = BlockEntry(
                    content=content, revision=entry.revision, tokens=count_tokens(content)
                )
            except KeyNotFoundError:
                data = default_content.encode()
                rev = await kv.create(key, data)
                mem.blocks[block.value] = BlockEntry(
                    content=default_content, revision=rev, tokens=count_tokens(default_content)
                )
            except Exception as exc:
                logger.error("Bootstrap failed for %s/%s: %s", agent_id, block.value, exc)

        logger.info(
            "Bootstrapped agent %s core memory (%d tokens)",
            agent_id, mem.total_tokens,
        )
        return mem

    async def delete_agent(self, agent_id: str) -> int:
        """Purge all blocks for an agent. Returns count of deleted keys."""
        kv = await self.ensure_bucket()
        deleted = 0
        for block in Block:
            key = self._key(agent_id, block)
            try:
                await kv.purge(key)
                deleted += 1
            except Exception:
                pass
        logger.info("Deleted %d blocks for agent %s", deleted, agent_id)
        return deleted

