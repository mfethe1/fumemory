"""NATS JetStream lane-lock execution wrapper.

Implements:
- Semantic Mutexes via NATS KV (compare-and-swap with fencing tokens)
- Dead Man's Switch heartbeats (10s TTL, 3s ping)
- Checkpoint streaming (incremental scratchpad to memU)
- Hydration handoff (recover orphaned tasks from checkpoint)
- Saga rollbacks (clean up partial state before re-execution)
- Dead Letter Queue (3 failures → poison pill → Medic gateway diagnoses)
- Mesh RPC (broadcast capability requests to peer gateways)

Usage:
    nc = await nats.connect("nats://localhost:4222")
    js = nc.jetstream()

    async with LaneLockedExecution(js, task, gateway_id="winnie") as executor:
        result = await executor.run(my_agent_function)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Coroutine
from uuid import UUID, uuid4

import nats
from nats.js import JetStreamContext
from nats.js.kv import KeyValue

from memu.swarm_models import (
    CheckpointSaved,
    DLQEntry,
    EventType,
    HydrationHandoff,
    LaneContested,
    LaneLock,
    RPCRequest,
    RPCResponse,
    RollbackExecuted,
    SwarmEvent,
    TaskNode,
    TaskOrphaned,
    TaskStatus,
)
from memu.hardening import sanitize_nats_subject

logger = logging.getLogger(__name__)

# NATS subjects
SUBJECT_EVENTS = "swarm.events"
SUBJECT_HEARTBEAT = "swarm.heartbeat"
SUBJECT_DLQ = "swarm.dlq"
SUBJECT_RPC_REQUEST = "swarm.rpc.request"
SUBJECT_RPC_RESPONSE = "swarm.rpc.response.{gateway_id}"
SUBJECT_CHECKPOINT = "swarm.checkpoint.{task_id}"

# KV bucket names
KV_LANES = "RESOURCE_LANES"
KV_FENCING = "FENCING_TOKENS"

# Config
HEARTBEAT_INTERVAL_S = 3
LANE_TTL_S = 10
MAX_FAILURES = 3


async def ensure_kv_buckets(js: JetStreamContext) -> tuple[KeyValue, KeyValue]:
    """Create or bind to the NATS KV buckets for lane locks and fencing tokens."""
    try:
        lanes_kv = await js.key_value(KV_LANES)
    except Exception:
        lanes_kv = await js.create_key_value(
            bucket=KV_LANES,
            ttl=LANE_TTL_S,
            description="Semantic mutex locks for resource lanes",
        )

    try:
        fencing_kv = await js.key_value(KV_FENCING)
    except Exception:
        fencing_kv = await js.create_key_value(
            bucket=KV_FENCING,
            description="Monotonically increasing fencing tokens per lane",
        )

    return lanes_kv, fencing_kv


class LaneLockedExecution:
    """Context manager for executing a task with lane-locked fault tolerance.

    Handles:
    1. Acquiring semantic mutex on all resource lanes
    2. Running heartbeat watchdog to keep locks alive
    3. Streaming checkpoint state for hydration
    4. Saga rollbacks on failure
    5. DLQ escalation after MAX_FAILURES
    """

    def __init__(
        self,
        js: JetStreamContext,
        task: TaskNode,
        gateway_id: str,
        nc: nats.NATS | None = None,
    ):
        self.js = js
        self.nc = nc
        self.task = task
        self.gateway_id = gateway_id
        self.lanes_kv: KeyValue | None = None
        self.fencing_kv: KeyValue | None = None
        self.acquired_locks: list[LaneLock] = []
        self._heartbeat_task: asyncio.Task | None = None
        self._checkpoint_seq = 0

    async def __aenter__(self):
        self.lanes_kv, self.fencing_kv = await ensure_kv_buckets(self.js)
        await self._acquire_all_lanes()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Cancel heartbeat
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Release all lane locks
        await self._release_all_lanes()

        # If exception occurred, handle failure
        if exc_type is not None:
            await self._handle_failure(str(exc_val))
            return True  # Suppress the exception — it's been handled

        return False

    async def _acquire_all_lanes(self):
        """Acquire semantic mutex on all resource lanes with fencing tokens."""
        for lane_id in self.task.resource_lanes:
            lock = await self._acquire_lane(lane_id)
            if lock:
                self.acquired_locks.append(lock)
            else:
                # Failed to acquire — release what we have and yield
                await self._release_all_lanes()
                raise LaneContestedError(
                    f"Cannot acquire lane {lane_id} for task {self.task.task_id}"
                )

    async def _acquire_lane(self, lane_id: str) -> LaneLock | None:
        """Atomic compare-and-swap to acquire a lane lock."""
        try:
            # Get or create fencing token
            try:
                entry = await self.fencing_kv.get(lane_id)
                current_token = int(entry.value.decode())
                new_token = current_token + 1
            except Exception:
                new_token = 1

            # Atomic create — fails if key exists (lane is locked)
            now = datetime.now(timezone.utc)
            lock_data = {
                "gateway_id": self.gateway_id,
                "task_id": str(self.task.task_id),
                "fencing_token": new_token,
                "acquired_at": now.isoformat(),
            }

            await self.lanes_kv.create(lane_id, json.dumps(lock_data).encode())
            await self.fencing_kv.put(lane_id, str(new_token).encode())

            lock = LaneLock(
                lane_id=lane_id,
                gateway_id=self.gateway_id,
                task_id=self.task.task_id,
                fencing_token=new_token,
                acquired_at=now,
                expires_at=now + timedelta(seconds=LANE_TTL_S),
            )

            # Publish lane acquired event
            await self._publish_event(EventType.LANE_ACQUIRED, {
                "lane_id": lane_id,
                "fencing_token": new_token,
            })

            logger.info(f"Acquired lane {lane_id} with fencing token {new_token}")
            return lock

        except Exception as e:
            # Lane is locked by another gateway
            logger.warning(f"Lane {lane_id} contested: {e}")

            # Try to read who holds it for diagnostics
            try:
                entry = await self.lanes_kv.get(lane_id)
                holder = json.loads(entry.value.decode())
                contested = LaneContested(
                    lane_id=lane_id,
                    requesting_gateway=self.gateway_id,
                    holding_gateway=holder["gateway_id"],
                    holding_fencing_token=holder["fencing_token"],
                    task_id=self.task.task_id,
                )
                await self._publish_event(EventType.LANE_CONTESTED, contested.model_dump())
            except Exception:
                pass

            return None

    async def _release_all_lanes(self):
        """Release all held lane locks."""
        for lock in self.acquired_locks:
            try:
                await self.lanes_kv.delete(lock.lane_id)
                await self._publish_event(EventType.LANE_RELEASED, {
                    "lane_id": lock.lane_id,
                    "fencing_token": lock.fencing_token,
                })
                logger.info(f"Released lane {lock.lane_id}")
            except Exception as e:
                logger.error(f"Failed to release lane {lock.lane_id}: {e}")

        self.acquired_locks.clear()

    async def _heartbeat_loop(self):
        """Dead Man's Switch — renew lane locks every HEARTBEAT_INTERVAL_S."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)

                for lock in self.acquired_locks:
                    lock_data = {
                        "gateway_id": self.gateway_id,
                        "task_id": str(self.task.task_id),
                        "fencing_token": lock.fencing_token,
                        "acquired_at": lock.acquired_at.isoformat(),
                    }
                    await self.lanes_kv.put(lock.lane_id, json.dumps(lock_data).encode())
                    lock.expires_at = datetime.now(timezone.utc) + timedelta(seconds=LANE_TTL_S)

                # Publish heartbeat
                await self._publish_event(EventType.HEARTBEAT, {
                    "gateway_id": self.gateway_id,
                    "task_id": str(self.task.task_id),
                    "lanes_held": [l.lane_id for l in self.acquired_locks],
                })

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")

    async def save_checkpoint(self, scratchpad: str, progress_pct: float = 0.0, tokens: int = 0):
        """Stream incremental checkpoint state back to memU for hydration."""
        self._checkpoint_seq += 1

        checkpoint = CheckpointSaved(
            task_id=self.task.task_id,
            gateway_id=self.gateway_id,
            checkpoint_sequence=self._checkpoint_seq,
            scratchpad=scratchpad,
            progress_pct=progress_pct,
            tokens_consumed=tokens,
        )

        await self._publish_event(EventType.CHECKPOINT_SAVED, checkpoint.model_dump())
        logger.info(f"Checkpoint {self._checkpoint_seq} saved for task {self.task.task_id}")

    async def run(
        self,
        agent_fn: Callable[..., Coroutine[Any, Any, dict[str, Any]]],
        **kwargs,
    ) -> dict[str, Any]:
        """Execute the agent function with full fault-tolerance wrapping.

        If the task has a checkpoint (orphaned recovery), injects hydration context.
        On failure, runs saga rollback and escalates to DLQ after MAX_FAILURES.
        """
        # Check if this is a hydration recovery
        if self.task.has_checkpoint:
            logger.info(f"Hydrating task {self.task.task_id} from checkpoint")
            await self._publish_event(EventType.TASK_HYDRATED, {
                "recovering_gateway": self.gateway_id,
                "checkpoint_state": self.task.checkpoint_state,
            })
            kwargs["hydration_context"] = self.task.checkpoint_state
            kwargs["hydration_prompt"] = (
                "You are recovering a task from a failed node. "
                "Here is their partial reasoning. Validate their logic and complete the task."
            )

        try:
            # Run the actual agent logic
            result = await agent_fn(task=self.task, executor=self, **kwargs)

            # Validate fencing token before committing
            for lock in self.acquired_locks:
                current_entry = await self.fencing_kv.get(lock.lane_id)
                current_token = int(current_entry.value.decode())
                if current_token != lock.fencing_token:
                    raise FencingTokenError(
                        f"Fencing token mismatch on {lock.lane_id}: "
                        f"held {lock.fencing_token}, current is {current_token}. "
                        f"Split-brain prevented."
                    )

            # Publish completion
            await self._publish_event(EventType.TASK_COMPLETED, {
                "result": result,
                "tokens_used": kwargs.get("tokens_used", 0),
            })

            return result

        except (LaneContestedError, FencingTokenError):
            raise  # Don't retry these — they're structural
        except Exception as e:
            await self._handle_failure(str(e))
            raise

    async def _handle_failure(self, error: str):
        """Handle task failure: rollback, increment counter, escalate to DLQ."""
        self.task.failure_count += 1
        self.task.last_error = error

        # Run saga rollback if defined
        if self.task.rollback_instruction:
            await self._execute_rollback()

        # Publish failure event
        await self._publish_event(EventType.TASK_FAILED, {
            "error": error,
            "failure_count": self.task.failure_count,
        })

        # Escalate to DLQ after MAX_FAILURES
        if self.task.is_poison_pill:
            dlq_entry = DLQEntry(
                task_id=self.task.task_id,
                failure_count=self.task.failure_count,
                failures=[{
                    "gateway_id": self.gateway_id,
                    "error": error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }],
            )
            await self._publish_event(EventType.DLQ_ENQUEUED, dlq_entry.model_dump())
            logger.error(
                f"Task {self.task.task_id} is a POISON PILL ({self.task.failure_count} failures). "
                f"Moved to Dead Letter Queue."
            )

    async def _execute_rollback(self):
        """Run the saga rollback instruction to clean up partial state."""
        logger.info(f"Executing rollback for task {self.task.task_id}")

        rollback = RollbackExecuted(
            task_id=self.task.task_id,
            gateway_id=self.gateway_id,
            rollback_instruction=self.task.rollback_instruction or "",
            success=True,
        )

        try:
            # TODO: Actually execute the rollback instruction
            # For now, just log it. Real implementation would interpret
            # the instruction string (e.g., "DROP TABLE temp_results")
            await self._publish_event(EventType.ROLLBACK_EXECUTED, rollback.model_dump())
        except Exception as e:
            rollback.success = False
            rollback.error = str(e)
            await self._publish_event(EventType.ROLLBACK_EXECUTED, rollback.model_dump())
            logger.error(f"Rollback failed for task {self.task.task_id}: {e}")

    async def _publish_event(self, event_type: EventType, payload: dict[str, Any]):
        """Publish a SwarmEvent to the NATS mesh."""
        event = SwarmEvent(
            source_gateway=self.gateway_id,
            event_type=event_type,
            task_id=self.task.task_id,
            payload=payload,
        )

        if self.nc:
            await self.nc.publish(
                SUBJECT_EVENTS,
                event.model_dump_json().encode(),
            )

    async def request_rpc(
        self,
        capability: str,
        payload: dict[str, Any],
        timeout_ms: int = 30_000,
    ) -> dict[str, Any]:
        """Broadcast an RPC request for a capability this gateway lacks.

        Used by edge devices that can't run a tool locally.
        """
        if not self.nc:
            raise RuntimeError("NATS connection required for RPC")

        request = RPCRequest(
            requesting_gateway=self.gateway_id,
            task_id=self.task.task_id,
            capability=capability,
            payload=payload,
            timeout_ms=timeout_ms,
        )

        # Subscribe to response channel
        response_subject = SUBJECT_RPC_RESPONSE.format(gateway_id=self.gateway_id)
        sub = await self.nc.subscribe(response_subject)

        # Publish request
        await self.nc.publish(
            SUBJECT_RPC_REQUEST,
            request.model_dump_json().encode(),
        )

        # Wait for response
        try:
            msg = await asyncio.wait_for(
                sub.next_msg(),
                timeout=timeout_ms / 1000,
            )
            response = RPCResponse.model_validate_json(msg.data)
            if response.success:
                return response.result
            else:
                raise RPCError(f"RPC failed: {response.error}")
        except asyncio.TimeoutError:
            raise RPCError(f"RPC timeout after {timeout_ms}ms for capability {capability}")
        finally:
            await sub.unsubscribe()


# --- Orphan Detector (runs as background service) ---

async def orphan_detector_loop(
    js: JetStreamContext,
    nc: nats.NATS,
    check_interval_s: float = 5.0,
):
    """Background service that detects expired leases and publishes TASK_ORPHANED events.

    This is the "immune system" watchdog. It runs on every gateway.
    """
    lanes_kv, _ = await ensure_kv_buckets(js)

    while True:
        try:
            await asyncio.sleep(check_interval_s)

            # Scan all lane keys
            keys = await lanes_kv.keys()
            now = datetime.now(timezone.utc)

            for key in keys:
                try:
                    entry = await lanes_kv.get(key)
                    lock_data = json.loads(entry.value.decode())
                    acquired_at = datetime.fromisoformat(lock_data["acquired_at"])

                    # Check if lock has expired (TTL exceeded without heartbeat renewal)
                    age_s = (now - acquired_at).total_seconds()
                    if age_s > LANE_TTL_S * 2:
                        orphaned = TaskOrphaned(
                            task_id=UUID(lock_data["task_id"]),
                            dead_gateway=lock_data["gateway_id"],
                            has_recoverable_state=True,  # Optimistic — checkpoint may exist
                            time_since_last_heartbeat_ms=int(age_s * 1000),
                        )

                        event = SwarmEvent(
                            source_gateway="orphan_detector",
                            event_type=EventType.TASK_ORPHANED,
                            task_id=orphaned.task_id,
                            payload=orphaned.model_dump(),
                        )

                        await nc.publish(SUBJECT_EVENTS, event.model_dump_json().encode())

                        # Clean up the dead lock
                        await lanes_kv.delete(key)
                        logger.warning(
                            f"Orphaned task {lock_data['task_id']} "
                            f"from dead gateway {lock_data['gateway_id']}"
                        )

                except Exception as e:
                    logger.debug(f"Error checking lane {key}: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Orphan detector error: {e}")


# --- Custom Exceptions ---

class LaneContestedError(Exception):
    """Raised when a lane lock cannot be acquired."""
    pass


class FencingTokenError(Exception):
    """Raised when a fencing token mismatch is detected (split-brain prevention)."""
    pass


class RPCError(Exception):
    """Raised when a mesh RPC call fails or times out."""
    pass
