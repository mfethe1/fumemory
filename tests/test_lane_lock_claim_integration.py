import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest


NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")


async def _connect_nats():
    return await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=1.0), timeout=2.0)


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
        try:
            nc = await _connect_nats()
        except Exception:
            pytest.skip(f"NATS unavailable at {NATS_URL}")

        js = nc.jetstream()
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

    finally:
        if nc is not None:
            await nc.drain()
            await nc.close()


def test_lane_lock_real_task_claim_contention():
    """Run two claimers for the same lane and verify exactly one winner."""
    asyncio.run(_test_lane_lock_real_task_claim_contention())


async def _test_lane_lock_real_task_claim_contention():
    try:
        import nats  # type: ignore
    except Exception:
        pytest.skip("nats-py is not installed; install optional test dependency")

    (
        _DuplicateClaimError,
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
            title=f"lane-lock-contend-{task_id}",
            status=TaskStatus.CLAIMED,
            description="2-claimer lane contention test",
            resource_lanes=[lane],
            compute_budget=0.0,
            rollback_instruction="noop",
        )

    async def claim_once(task_id: UUID, lane: str, gateway_id: str) -> str:
        async with LaneLockedExecution(js, build_task(task_id, lane), gateway_id, nc):
            return gateway_id

    nc = None
    lanes_kv = None
    fencing_kv = None
    lane: str | None = None
    try:
        try:
            nc = await _connect_nats()
        except Exception:
            pytest.skip(f"NATS unavailable at {NATS_URL}")

        js = nc.jetstream()
        lanes_kv, fencing_kv = await ensure_kv_buckets(js)

        task_id = uuid4()
        lane = f"integration-contend-{uuid4()}"
        winners: list[str] = []
        losers: list[str] = []
        start_gate = asyncio.Event()

        await lanes_kv.delete(lane)
        await fencing_kv.delete(lane)

        async def contender(gateway_id: str):
            await start_gate.wait()
            try:
                await claim_once(task_id, lane, gateway_id)
                winners.append(gateway_id)
            except LaneContestedError:
                losers.append(gateway_id)
            except Exception as exc:  # pragma: no cover
                pytest.fail(f"contender {gateway_id} failed unexpectedly: {exc}")

        contender_tasks = [
            asyncio.create_task(contender("gateway-a")),
            asyncio.create_task(contender("gateway-b")),
        ]

        start_gate.set()
        await asyncio.gather(*contender_tasks)

        assert len(winners) == 1, f"expected 1 winner, got {winners}"
        assert len(losers) == 1, f"expected 1 loser, got {losers}"
        assert set(winners).isdisjoint(set(losers))

    finally:
        if lanes_kv is not None and lane is not None:
            await lanes_kv.delete(lane)
        if fencing_kv is not None and lane is not None:
            await fencing_kv.delete(lane)

        if nc is not None:
            await nc.drain()
            await nc.close()


def test_lane_lock_ttl_heartbeat_keeps_lock_alive():
    """Verify heartbeat renewal prevents stale reclaim while claimant is running."""
    asyncio.run(_test_lane_lock_ttl_heartbeat_keeps_lock_alive())


async def _test_lane_lock_ttl_heartbeat_keeps_lock_alive():
    try:
        import nats  # type: ignore
    except Exception:
        pytest.skip("nats-py is not installed; install optional test dependency")

    (
        _DuplicateClaimError,
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
            title=f"lane-lock-heartbeat-{task_id}",
            status=TaskStatus.CLAIMED,
            description="lock heartbeat renewal test",
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
    lane: str | None = None
    try:
        try:
            nc = await _connect_nats()
        except Exception:
            pytest.skip(f"NATS unavailable at {NATS_URL}")

        js = nc.jetstream()
        lanes_kv, fencing_kv = await ensure_kv_buckets(js)

        task_id = uuid4()
        lane = f"integration-heartbeat-{uuid4()}"
        started = asyncio.Event()

        await lanes_kv.delete(lane)
        await fencing_kv.delete(lane)

        async def holder():
            async with LaneLockedExecution(js, build_task(task_id, lane), "gateway-owner", nc):
                started.set()
                await asyncio.sleep(12)

        holder_task = asyncio.create_task(holder())
        await asyncio.wait_for(started.wait(), timeout=5.0)

        # Before TTL expiry and past nominal TTL, lock should still be held due heartbeat.
        await asyncio.sleep(9)
        with pytest.raises(LaneContestedError):
            await claim_once(task_id, lane, "gateway-contender")

        await asyncio.sleep(5)
        with pytest.raises(LaneContestedError):
            await claim_once(task_id, lane, "gateway-contender")

    finally:
        if lanes_kv is not None and lane is not None:
            await lanes_kv.delete(lane)
        if fencing_kv is not None and lane is not None:
            await fencing_kv.delete(lane)

        if nc is not None:
            await nc.drain()
            await nc.close()
