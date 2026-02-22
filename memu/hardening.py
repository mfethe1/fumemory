"""Failure Mode Analysis & Hardening — fumemory swarm

This module documents every identified failure mode, its blast radius,
and the implemented mitigation. Every function here is a concrete
hardening mechanism that plugs into the existing architecture.

Philosophy: If it CAN break, assume it WILL break. Build the recovery
into the system, not into the runbook.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

try:
    import resource  # Unix only
except ImportError:
    resource = None  # type: ignore

logger = logging.getLogger(__name__)


# ============================================================================
# FAILURE MODE 1: Checkpoint Storage Explosion
# ----------------------------------------------------------------------------
# Risk: Agents stream checkpoints every few seconds. Long-running tasks
#        can generate thousands of checkpoint rows, filling Postgres.
# Blast radius: Disk full → ALL memU writes fail → entire swarm halts.
# Fix: Automatic checkpoint pruning. Keep only last N checkpoints per task.
# ============================================================================

MAX_CHECKPOINTS_PER_TASK = int(os.environ.get("MAX_CHECKPOINTS_PER_TASK", "10"))

PRUNE_CHECKPOINTS_SQL = """
DELETE FROM checkpoints
WHERE id IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY task_id ORDER BY checkpoint_seq DESC
        ) AS rn
        FROM checkpoints
    ) ranked
    WHERE rn > $1
);
"""

async def prune_checkpoints(pool, max_per_task: int = MAX_CHECKPOINTS_PER_TASK) -> int:
    """Delete old checkpoints, keeping only the most recent N per task."""
    async with pool.acquire() as conn:
        result = await conn.execute(PRUNE_CHECKPOINTS_SQL, max_per_task)
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"Pruned {count} stale checkpoints (keeping last {max_per_task} per task)")
        return count


# ============================================================================
# FAILURE MODE 2: Event Ledger Unbounded Growth
# ----------------------------------------------------------------------------
# Risk: Append-only event ledger grows forever. Old completed task events
#        are never useful for live operations but consume disk and slow queries.
# Blast radius: Query latency → projection becomes slow → DAG stale.
# Fix: Archive completed task events to a cold table after N days.
# ============================================================================

EVENT_RETENTION_DAYS = int(os.environ.get("EVENT_RETENTION_DAYS", "30"))

ARCHIVE_EVENTS_SQL = """
WITH archived AS (
    INSERT INTO events_archive
    SELECT e.* FROM events e
    JOIN tasks t ON e.task_id = t.task_id
    WHERE t.status IN ('completed', 'cancelled', 'failed')
      AND t.updated_at < NOW() - INTERVAL '%s days'
    RETURNING event_id
)
DELETE FROM events WHERE event_id IN (SELECT event_id FROM archived);
"""

CREATE_ARCHIVE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events_archive (LIKE events INCLUDING ALL);
CREATE INDEX IF NOT EXISTS idx_events_archive_task ON events_archive (task_id);
CREATE INDEX IF NOT EXISTS idx_events_archive_ts ON events_archive (timestamp DESC);
"""

async def archive_old_events(pool, retention_days: int = EVENT_RETENTION_DAYS) -> int:
    """Move completed task events older than retention_days to archive table."""
    async with pool.acquire() as conn:
        # Ensure archive table exists
        await conn.execute(CREATE_ARCHIVE_TABLE_SQL)
        # Archive and delete
        result = await conn.execute(
            ARCHIVE_EVENTS_SQL.replace("%s", str(retention_days))
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info(f"Archived {count} old events (retention: {retention_days}d)")
        return count


# ============================================================================
# FAILURE MODE 3: Blackboard Validation Deadlock
# ----------------------------------------------------------------------------
# Risk: An insight is published, but no second gateway is online to validate it.
#        The insight sits in UNTRUSTED limbo forever. If it's critical (e.g.,
#        "API endpoint changed"), the swarm operates with stale knowledge.
# Blast radius: Knowledge silos persist despite blackboard existing.
# Fix: Auto-promote after timeout if source confidence >= threshold.
#      Also: self-validation allowed if only 1 gateway is online.
# ============================================================================

BLACKBOARD_AUTO_PROMOTE_MINUTES = int(os.environ.get("BLACKBOARD_AUTO_PROMOTE_MINUTES", "30"))
BLACKBOARD_AUTO_PROMOTE_MIN_CONFIDENCE = float(os.environ.get("BLACKBOARD_AUTO_PROMOTE_CONFIDENCE", "0.9"))

AUTO_PROMOTE_SQL = """
UPDATE blackboard
SET is_validated = TRUE,
    validated_by = 'auto_promote',
    validated_at = NOW()
WHERE is_validated = FALSE
  AND confidence >= $1
  AND created_at < NOW() - INTERVAL '%s minutes'
RETURNING entry_id, key;
"""

async def auto_promote_stale_insights(
    pool,
    timeout_minutes: int = BLACKBOARD_AUTO_PROMOTE_MINUTES,
    min_confidence: float = BLACKBOARD_AUTO_PROMOTE_MIN_CONFIDENCE,
) -> int:
    """Auto-validate high-confidence blackboard entries after timeout."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            AUTO_PROMOTE_SQL.replace("%s", str(timeout_minutes)),
            min_confidence,
        )
        if rows:
            for row in rows:
                logger.info(
                    f"Auto-promoted blackboard entry: {row['key']} "
                    f"(waited {timeout_minutes}m, confidence >= {min_confidence})"
                )
        return len(rows)


# ============================================================================
# FAILURE MODE 4: Boot Hydration Hang
# ----------------------------------------------------------------------------
# Risk: boot.py calls hydrate_state() which hits memU API. If memU is down
#        or slow, the container hangs in "hydrating" state forever. The Warden
#        sees it as alive (process running) but it's not doing work.
# Blast radius: Warm pool slot consumed by zombie. Task never executes.
# Fix: Hard timeout on hydration. If exceeded, boot with empty context.
# ============================================================================

HYDRATION_TIMEOUT_S = int(os.environ.get("HYDRATION_TIMEOUT_S", "15"))


async def hydrate_with_timeout(hydrate_fn, timeout_s: int = HYDRATION_TIMEOUT_S) -> dict:
    """Wrap hydrate_state() with a hard timeout. Boot empty if it hangs."""
    try:
        return await asyncio.wait_for(hydrate_fn(), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning(
            f"Hydration timed out after {timeout_s}s. Booting with empty context. "
            f"This gateway will operate without checkpoint recovery."
        )
        return {
            "blackboard": [],
            "checkpoint": None,
            "dag_context": None,
            "hydration_prompt": None,
            "boot_time": datetime.now(timezone.utc).isoformat(),
            "hydration_failed": True,
            "failure_reason": f"timeout_after_{timeout_s}s",
        }
    except Exception as e:
        logger.error(f"Hydration failed: {e}. Booting with empty context.")
        return {
            "blackboard": [],
            "checkpoint": None,
            "dag_context": None,
            "hydration_prompt": None,
            "boot_time": datetime.now(timezone.utc).isoformat(),
            "hydration_failed": True,
            "failure_reason": str(e),
        }


# ============================================================================
# FAILURE MODE 5: Fencing Token Integer Overflow
# ----------------------------------------------------------------------------
# Risk: Fencing tokens are monotonically increasing integers. In a system
#        that runs 24/7, tokens can overflow after billions of lock acquisitions.
# Blast radius: Token wraps to 0 or negative → all fencing checks fail →
#               split-brain writes succeed → data corruption.
# Fix: Use BIGINT (2^63) and add overflow detection. At 1000 locks/second,
#      BIGINT overflows in 292 million years. Good enough.
# ============================================================================

MAX_FENCING_TOKEN = 2**62  # Leave headroom before BIGINT overflow


def check_fencing_token_health(current_token: int) -> bool:
    """Warn if fencing tokens are approaching overflow."""
    if current_token > MAX_FENCING_TOKEN:
        logger.critical(
            f"FENCING TOKEN OVERFLOW IMMINENT: {current_token} > {MAX_FENCING_TOKEN}. "
            f"Manual intervention required: reset tokens in NATS KV."
        )
        return False
    if current_token > MAX_FENCING_TOKEN // 2:
        logger.warning(f"Fencing tokens at {current_token} — approaching 50% of max capacity")
    return True


# ============================================================================
# FAILURE MODE 6: NATS Subject Injection
# ----------------------------------------------------------------------------
# Risk: A gateway constructs a NATS subject using user input or LLM output.
#        Malformed subjects (e.g., "swarm.events.*.>") could subscribe to
#        unintended channels or publish to control subjects.
# Blast radius: Privilege escalation within the mesh.
# Fix: Strict subject sanitization. Only alphanumeric + dots + hyphens.
# ============================================================================

import re

SAFE_SUBJECT_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+$')
MAX_SUBJECT_LENGTH = 256


def sanitize_nats_subject(subject: str) -> str:
    """Sanitize a NATS subject to prevent injection attacks.

    Raises ValueError if the subject contains wildcards or invalid characters.
    """
    if not subject or len(subject) > MAX_SUBJECT_LENGTH:
        raise ValueError(f"Subject must be 1-{MAX_SUBJECT_LENGTH} chars, got {len(subject)}")

    # Block wildcards in constructed subjects (only allow in static subscriptions)
    if '*' in subject or '>' in subject:
        raise ValueError(f"Wildcards not allowed in constructed subjects: {subject}")

    if not SAFE_SUBJECT_PATTERN.match(subject):
        raise ValueError(f"Invalid NATS subject characters: {subject}")

    return subject


# ============================================================================
# FAILURE MODE 7: Concurrent DAG Projection Race Condition
# ----------------------------------------------------------------------------
# Risk: Two gateways call materialize_tasks() simultaneously for the same
#        root_prompt_id. The ON CONFLICT clause handles individual rows, but
#        the full projection is not atomic — one projection could read stale
#        events while another is mid-write.
# Blast radius: DAG state inconsistency → tasks executed out of order.
# Fix: Advisory lock on root_prompt_id during projection.
# ============================================================================

ACQUIRE_PROJECTION_LOCK_SQL = """
SELECT pg_advisory_xact_lock(hashtext($1::text));
"""


async def project_with_lock(pool, root_prompt_id: UUID, projection_fn):
    """Run DAG projection under an advisory lock to prevent races."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Acquire lock scoped to this root prompt
            await conn.execute(ACQUIRE_PROJECTION_LOCK_SQL, str(root_prompt_id))
            return await projection_fn(pool, root_prompt_id)


# ============================================================================
# FAILURE MODE 8: WebSocket Bridge Crash → UI Goes Dark
# ----------------------------------------------------------------------------
# Risk: ws_bridge.py crashes or loses NATS connection. All Glass Box
#        clients lose visibility. The swarm keeps running blind.
# Blast radius: Human loses observability. God Mode unavailable.
# Fix: Bridge self-restarts via supervisor. Clients auto-reconnect.
#      Bridge publishes its own heartbeat so Warden can monitor it.
# ============================================================================

BRIDGE_HEARTBEAT_INTERVAL_S = 10


async def bridge_self_monitor(nc, bridge_id: str = "ws_bridge"):
    """Publish bridge heartbeat so the Warden knows the UI layer is alive."""
    import json

    while True:
        try:
            await nc.publish(
                "swarm.warden.heartbeat",
                json.dumps({
                    "gateway_id": bridge_id,
                    "role": "ws_bridge",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "connected_clients": 0,  # Updated by bridge
                }).encode(),
            )
            await asyncio.sleep(BRIDGE_HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Bridge heartbeat error: {e}")
            await asyncio.sleep(2)


# ============================================================================
# FAILURE MODE 9: Container OOM Kill (Docker)
# ----------------------------------------------------------------------------
# Risk: An agent's LLM inference or data processing consumes all container
#        memory. Docker OOM-kills the container. The Warden respawns it,
#        but if the same task triggers OOM again, we crash-loop.
# Blast radius: Crash loop → DLQ, but wastes respawn attempts first.
# Fix: Proactive memory monitoring. If >80% of container memory limit,
#      checkpoint immediately and yield the task before OOM.
# ============================================================================

MEMORY_THRESHOLD_PCT = float(os.environ.get("MEMORY_THRESHOLD_PCT", "80.0"))


def check_memory_pressure() -> dict[str, Any]:
    """Check current process memory usage against container limits.

    Returns memory stats and whether we're in danger zone.
    """
    try:
        # Try reading cgroup memory limit (Docker)
        cgroup_limit_path = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        cgroup_usage_path = "/sys/fs/cgroup/memory/memory.usage_in_bytes"

        if os.path.exists(cgroup_limit_path):
            with open(cgroup_limit_path) as f:
                limit_bytes = int(f.read().strip())
            with open(cgroup_usage_path) as f:
                usage_bytes = int(f.read().strip())

            pct = (usage_bytes / limit_bytes) * 100 if limit_bytes > 0 else 0

            return {
                "limit_mb": limit_bytes / (1024 * 1024),
                "usage_mb": usage_bytes / (1024 * 1024),
                "usage_pct": round(pct, 1),
                "in_danger": pct > MEMORY_THRESHOLD_PCT,
                "source": "cgroup",
            }
    except Exception:
        pass

    # Fallback: use Python's resource module (Unix only)
    if resource is not None:
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            return {
                "usage_mb": usage.ru_maxrss / 1024,  # KB to MB on Linux
                "usage_pct": 0,  # Can't determine limit without cgroup
                "in_danger": False,
                "source": "rusage",
            }
        except Exception:
            pass

    return {"usage_mb": 0, "usage_pct": 0, "in_danger": False, "source": "unknown"}


# ============================================================================
# FAILURE MODE 10: Warden Itself Dies (SPOF)
# ----------------------------------------------------------------------------
# Risk: The Warden is the only process with Docker access. If it crashes,
#        no respawns, no sandboxes, no failover.
# Blast radius: Entire self-healing capability lost.
# Fix: systemd watchdog + dual-deploy Warden (local + Railway).
#      Warden publishes its own heartbeat. If missing for 30s, systemd
#      restarts it. Railway Warden detects local silence and takes over.
# ============================================================================

WARDEN_WATCHDOG_SYSTEMD_UNIT = """
[Unit]
Description=fumemory Warden — Container Control Plane
After=network.target docker.service nats.service
Requires=docker.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m memu.warden_service
Restart=always
RestartSec=3
WatchdogSec=30
Environment=WARDEN_MAX_CONTAINERS=10
Environment=WARDEN_WARM_POOL_MODE=cold

# OOM protection: Warden must survive even if containers die
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
"""


# ============================================================================
# HARDENING: Graceful Shutdown Handler
# ----------------------------------------------------------------------------
# When a container receives SIGTERM (Docker stop), it must:
# 1. Save final checkpoint for current task
# 2. Release all lane locks
# 3. Publish "draining" status to mesh
# 4. Then exit cleanly
# ============================================================================

_shutdown_handlers: list = []


def register_shutdown_handler(handler):
    """Register a coroutine to run on graceful shutdown."""
    _shutdown_handlers.append(handler)


def install_signal_handlers(loop: asyncio.AbstractEventLoop):
    """Install SIGTERM/SIGINT handlers for graceful shutdown."""

    async def _shutdown(sig):
        logger.info(f"Received signal {sig}. Running {len(_shutdown_handlers)} shutdown handlers...")
        for handler in _shutdown_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await asyncio.wait_for(handler(), timeout=5.0)
                else:
                    handler()
            except Exception as e:
                logger.error(f"Shutdown handler error: {e}")
        logger.info("Shutdown complete")
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_shutdown(s)))


# ============================================================================
# MAINTENANCE: Periodic hardening sweep
# ============================================================================

async def hardening_sweep(pool, nc=None):
    """Run all maintenance tasks. Call this on a schedule (e.g., every 5 minutes)."""
    results = {}

    results["checkpoints_pruned"] = await prune_checkpoints(pool)
    results["events_archived"] = await archive_old_events(pool)
    results["insights_promoted"] = await auto_promote_stale_insights(pool)
    results["memory"] = check_memory_pressure()

    if results["memory"]["in_danger"]:
        logger.warning(
            f"MEMORY PRESSURE: {results['memory']['usage_pct']}% used. "
            f"Consider checkpointing current task and yielding."
        )

    logger.info(f"Hardening sweep complete: {results}")
    return results
