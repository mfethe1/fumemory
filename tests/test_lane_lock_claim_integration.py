import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest


NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")


def _load_lane_modules():
    from memu.lane_lock import (
        DuplicateClaimError,
        LaneContestedError,
        LaneLockedExecution,
        ensure_kv_buckets,
    )
    from memu.swarm_models import TaskNode, TaskStatus
    return (
        DuplicateClaimError,
        LaneContestedError,
        LaneLockedExecution,
        ensure_kv_buckets,
        TaskNode,
        TaskStatus,
    )


def test_lane_lock_contention_and_stale_recovery():
    asyncio.run(_test_lane_lock_contention_and_stale_recovery())


async def _test_lane_lock_contention_and_stale_recovery():
    try:
        import nats  # type: ignore
    except Exception:
        pytest.skip("nats-py is not installed; install optional test dependency")

    (
        DuplicateClaimError,
        LaneContestedError,
        LaneLockedExecution,
        ensure_kv_buckets,
        TaskNode,
        TaskStatus,
    ) = _load_lane_modules()

    def build_task(task_id: UUID, lane: str) -> TaskNode:
        return TaskNode(
            task_id=task_id,
            root_prompt_id=task_id,
            title=f"lane-lock-test-{task_id}",
            status=TaskStatus.CLAIMED,
            description="integration lock contention test",
            resource_lanes=[lane],
            compute_budget=0.0,
            rollback_instruction="noop",
        )

    async def claim_once(task_id: UUID, lane: str, gateway_id: str) -> None:
        async with LaneLockedExecution(js, build_task(task_id, lane), gateway_id, nc):
            return None

    nc = None
    lanes_kv = None
    fencing_kv = None

    try:
        nc = await nats.connect(NATS_URL)
    except Exception:
        pytest.skip(f"NATS unavailable at {NATS_URL}")

    js = nc.jetstream()
    try:
        await js.account_info()
    except Exception as exc:
        pytest.skip(f"JetStream unavailable at {NATS_URL}: {exc}")

    lanes_kv, fencing_kv = await ensure_kv_buckets(js)

    lane = f"integration-lane-{uuid4()}"
    task_a = uuid4()
    task_b = uuid4()
    lock_held = asyncio.Event()
    holder_task = None

    try:
        await lanes_kv.delete(lane)
        await fencing_kv.delete(lane)

        async def holder():
            async with LaneLockedExecution(js, build_task(task_a, lane), "gateway-a", nc):
                lock_held.set()
                await asyncio.sleep(2.5)

        holder_task = asyncio.create_task(holder())
        await asyncio.wait_for(lock_held.wait(), timeout=5.0)

        # Contention path: different gateway cannot claim occupied lane.
        with pytest.raises(LaneContestedError):
            await claim_once(task_b, lane, "gateway-b")

        # Duplicate claim prevention: same gateway/task cannot reacquire same active lane.
        with pytest.raises(DuplicateClaimError):
            await claim_once(task_a, lane, "gateway-a")

        await holder_task

        # Stale recovery path: inject stale lock state and verify reclaim succeeds.
        stale_payload = {
            "gateway_id": "ghost-gateway",
            "task_id": str(task_a),
            "fencing_token": 999,
            "acquired_at": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat(),
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
            "lane_id": lane,
        }
        await lanes_kv.put(lane, json.dumps(stale_payload).encode())
        await claim_once(task_b, lane, "gateway-b")

    finally:
        if holder_task is not None and not holder_task.done():
            holder_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await holder_task

        if lanes_kv is not None:
            await lanes_kv.delete(lane)
        if fencing_kv is not None:
            await fencing_kv.delete(lane)

        if nc is not None:
            await nc.drain()
            await nc.close()
