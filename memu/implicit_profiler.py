"""Implicit Profiling Daemon — autonomously extracts user preferences from chat.

Subscribes to ``swarm.chat.*`` on NATS. Debounces incoming messages into
batches (by agent_id), then invokes a fast LLM to extract a JSON diff of
user preferences.  The diff is applied to the agent's ``User_Profile``
NATS KV block using CAS-safe writes.

This runs as a background asyncio task inside the memU API process, or
as a standalone worker via ``python -m memu.implicit_profiler``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from typing import Any

import httpx

from memu.cluster import NATSClusterManager
from memu.nats_publisher import tenant_subject
from memu.core_memory import (
    Block,
    CoreMemoryManager,
    count_tokens,
    token_safe_truncate,
    DEFAULT_TOKEN_LIMITS,
)

logger = logging.getLogger(__name__)

# --- Config ---

DEBOUNCE_SECONDS = float(os.environ.get("PROFILER_DEBOUNCE_S", "10.0"))
LLM_BASE_URL = os.environ.get("PROFILER_LLM_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("PROFILER_LLM_MODEL", "gpt-4o-mini")
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MAX_BATCH_CHARS = 4_000  # Max chars to send to LLM per batch
DLQ_TOPIC = "memu.dlq.llm_failures"
MAX_LLM_RETRIES = 3

SYSTEM_PROMPT = """\
You are a silent observer that extracts user preference signals from chat messages.

Given a batch of recent chat messages from a user, output a JSON object with ONLY
changed or newly-discovered preferences. Do NOT include unchanged fields.

Output schema (include only fields with new data):
{
  "communication_style": "concise|verbose|technical|casual",
  "preferred_tools": ["tool_name", ...],
  "expertise_areas": ["area", ...],
  "timezone": "...",
  "frustrations": ["..."],
  "preferences": {"key": "value", ...},
  "context_notes": "free-form note about the user"
}

If no new preferences are detectable, output: {}
"""


class ImplicitProfiler:
    """Background daemon that extracts user preferences from chat streams."""

    def __init__(
        self,
        cluster: NATSClusterManager,
        core_mem: CoreMemoryManager,
        tenant_id: str | None = None,
    ):
        self._cluster = cluster
        self._core_mem = core_mem
        self._tenant_id = tenant_id or os.environ.get("TENANT_ID") or None
        self._buffers: dict[str, list[str]] = defaultdict(list)
        self._flush_tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._subscription = None

    async def start(self) -> None:
        """Subscribe to swarm.chat.* and begin processing."""
        self._running = True
        nc = self._cluster.active_connection
        subject = tenant_subject(self._tenant_id, "swarm.chat.*")
        # Queue group: only one worker pod extracts preferences per chat batch
        self._subscription = await nc.subscribe(subject, cb=self._on_message, queue="memu-worker-group")
        logger.info("ImplicitProfiler started — listening on %s", subject)

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        if self._subscription:
            try:
                await self._subscription.unsubscribe()
            except Exception:
                pass
        for task in self._flush_tasks.values():
            task.cancel()
        self._flush_tasks.clear()
        logger.info("ImplicitProfiler stopped")

    async def _on_message(self, msg: Any) -> None:
        """NATS callback — buffer messages per agent, debounce flush."""
        try:
            payload = json.loads(msg.data.decode())
        except Exception:
            return

        agent_id = payload.get("agent_id") or msg.subject.split(".")[-1]
        content = payload.get("content", "")
        if not content or not agent_id:
            return

        # Append to buffer (truncate if too large)
        buf = self._buffers[agent_id]
        buf.append(content[:1_000])

        # Total buffer size check
        total = sum(len(m) for m in buf)
        if total > MAX_BATCH_CHARS:
            buf[:] = buf[-(MAX_BATCH_CHARS // 500):]  # keep most recent

        # Debounce: cancel existing timer, start new one
        existing = self._flush_tasks.get(agent_id)
        if existing and not existing.done():
            existing.cancel()
        self._flush_tasks[agent_id] = asyncio.create_task(
            self._debounced_flush(agent_id)
        )

    async def _debounced_flush(self, agent_id: str) -> None:
        """Wait for debounce period, then flush the buffer."""
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        buf = self._buffers.pop(agent_id, [])
        if not buf:
            return

        await self._extract_and_apply(agent_id, buf)

    async def _extract_and_apply(self, agent_id: str, messages: list[str]) -> None:
        """Call LLM to extract preferences, apply diff to User_Profile."""
        batch_text = "\n---\n".join(messages)

        # Call LLM
        diff = await self._call_llm(batch_text)
        if not diff:
            return

        # Read current profile
        entry = await self._core_mem.get_block(agent_id, Block.USER_PROFILE)
        current_profile: dict[str, Any] = {}
        if entry and entry.content.strip():
            try:
                current_profile = json.loads(entry.content)
            except json.JSONDecodeError:
                current_profile = {"raw": entry.content}

        # Merge diff into profile
        merged = self._merge_profile(current_profile, diff)
        merged_json = json.dumps(merged, indent=2)

        # Token-safe budget check: pop discrete dict keys until it fits
        tokens = count_tokens(merged_json)
        limit = DEFAULT_TOKEN_LIMITS[Block.USER_PROFILE]
        if tokens > limit:
            logger.warning(
                "Merged profile for %s exceeds limit (%d > %d tokens), truncating",
                agent_id, tokens, limit,
            )
            merged = token_safe_truncate(
                merged,
                limit,
                protected_keys={"communication_style", "preferred_tools", "expertise_areas", "timezone"},
            )
            merged_json = json.dumps(merged, indent=2)

        # CAS write
        try:
            revision = entry.revision if entry else 0
            await self._core_mem.update_block(
                agent_id,
                Block.USER_PROFILE,
                merged_json,
                revision,
                caller="worker",
            )
            logger.info(
                "Updated User_Profile for %s (diff keys: %s)",
                agent_id, list(diff.keys()),
            )
        except Exception as exc:
            logger.error("Failed to update User_Profile for %s: %s", agent_id, exc)

    @staticmethod
    def _merge_profile(current: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
        """Deep-merge diff into current profile. Lists are unioned, dicts are merged."""
        merged = current.copy()
        for key, value in diff.items():
            if key in merged:
                if isinstance(merged[key], list) and isinstance(value, list):
                    # Union lists, preserving order
                    existing = set(merged[key])
                    merged[key] = merged[key] + [v for v in value if v not in existing]
                elif isinstance(merged[key], dict) and isinstance(value, dict):
                    merged[key] = {**merged[key], **value}
                else:
                    merged[key] = value
            else:
                merged[key] = value
        return merged

    async def _call_llm(self, batch_text: str) -> dict[str, Any] | None:
        """Call fast LLM to extract preference diff with retry + DLQ on failure."""
        if not LLM_API_KEY:
            logger.debug("No LLM API key — skipping profile extraction")
            return None

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        }
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Recent chat messages:\n\n{batch_text}"},
            ],
            "max_tokens": 500,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        last_error = None
        for attempt in range(1, MAX_LLM_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{LLM_BASE_URL}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    # Rate limit → transient retry
                    if resp.status_code == 429:
                        retry_after = float(resp.headers.get("retry-after", "5"))
                        logger.warning(
                            "LLM rate limited (attempt %d/%d), retrying in %.1fs",
                            attempt, MAX_LLM_RETRIES, retry_after,
                        )
                        # TODO: If migrated to JetStream consumer, call
                        # msg.in_progress() during long sleeps to prevent
                        # AckWait timeout redelivery.
                        await asyncio.sleep(retry_after)
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    diff = json.loads(content)
                    if not isinstance(diff, dict):
                        raise json.JSONDecodeError("LLM returned non-dict", content, 0)
                    return diff if diff else None

            except json.JSONDecodeError as exc:
                last_error = exc
                logger.warning(
                    "LLM returned bad JSON (attempt %d/%d): %s",
                    attempt, MAX_LLM_RETRIES, exc,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s",
                    attempt, MAX_LLM_RETRIES, exc,
                )
            # Backoff between retries
            if attempt < MAX_LLM_RETRIES:
                await asyncio.sleep(1.0 * attempt)

        # All retries exhausted — publish to DLQ
        await self._publish_to_dlq(batch_text, str(last_error))
        return None

    async def _publish_to_dlq(self, batch_text: str, error: str) -> None:
        """Publish failed LLM payload to dead letter queue for later inspection."""
        try:
            nc = self._cluster.active_connection
            dlq_payload = json.dumps({
                "source": "implicit_profiler",
                "error": error,
                "batch_text_preview": batch_text[:500],
            }).encode()
            await nc.publish(DLQ_TOPIC, dlq_payload)
            logger.info("Published failed LLM payload to DLQ: %s", DLQ_TOPIC)
        except Exception as exc:
            logger.error("Failed to publish to DLQ: %s", exc)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

async def _run_standalone() -> None:
    """Run as standalone NATS worker."""
    from memu.nats_config import primary_nats_url
    import nats

    nc = await nats.connect(primary_nats_url())
    js = nc.jetstream()

    cluster = NATSClusterManager()
    await cluster.connect()

    core_mem = CoreMemoryManager(cluster.jetstream)
    await core_mem.ensure_bucket()

    profiler = ImplicitProfiler(cluster, core_mem)
    await profiler.start()

    # Block until interrupted
    stop = asyncio.Event()
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await profiler.stop()
        await cluster.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_standalone())
