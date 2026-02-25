#!/usr/bin/env python3
"""Run an offline-checkable dual-claim/stale-lock lane-lock drill with artifact output.

Produces a JSON artifact describing winners/losers and pass/fail for:
  1) dual claimant contention
  2) stale-lock recovery
  3) TTL heartbeat renewal protection
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


async def _run_drill() -> dict[str, Any]:
    try:
        import nats  # type: ignore
    except Exception:
        return {
            "ok": False,
            "phase": "dependency-missing",
            "reason": "nats-py not installed",
            "passes": {},
            "failures": ["nats-py dependency not installed"],
        }

    # Make project imports resolvable when run from repository root.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from memu.lane_lock import LaneContestedError, LaneLockedExecution, ensure_kv_buckets
    from memu.swarm_models import TaskNode, TaskStatus

    # Railway-first binding (no implicit localhost fallback).
    # Resolution order: explicit NATS_URL, then RAILWAY_NATS_URL.
    NATS_URL = os.getenv("NATS_URL") or os.getenv("RAILWAY_NATS_URL")
    if not NATS_URL:
        return {
            "ok": False,
            "phase": "config-missing",
            "reason": "NATS_URL/RAILWAY_NATS_URL not set (localhost fallback disabled)",
            "passes": {},
            "failures": ["missing broker endpoint env"],
        }
    lane_uuid = uuid4()

    passes = {}
    failures = []

    async def _build_task(task_id: UUID, lane: str) -> TaskNode:
        return TaskNode(
            task_id=task_id,
            root_prompt_id=task_id,
            title=f"lane-lock-drill-{task_id}",
            status=TaskStatus.CLAIMED,
            description="Dual-claim lane-lock drill",
            resource_lanes=[lane],
            compute_budget=0.0,
            rollback_instruction="noop",
        )

    async def _claim_once(js, nc, task_id: UUID, lane: str, gateway_id: str):
        async with LaneLockedExecution(js, await _build_task(task_id, lane), gateway_id, nc):
            return gateway_id

    nc = None
    lanes_kv = None
    fencing_kv = None
    lane = f"dual-claim-{lane_uuid}"
    task_id = uuid4()

    try:
        try:
            nc = await asyncio.wait_for(
                nats.connect(NATS_URL, allow_reconnect=False),
                timeout=2,
            )
        except Exception as exc:
            return {
                "ok": False,
                "phase": "connect-failed",
                "reason": f"Cannot connect to NATS at {NATS_URL}",
                "details": str(exc),
                "passes": {},
                "failures": ["nats unavailable"],
            }

        js = nc.jetstream()
        lanes_kv, fencing_kv = await ensure_kv_buckets(js)

        # Ensure clean state for this lane.
        await lanes_kv.delete(lane)
        await fencing_kv.delete(lane)

        # 1) Dual-claimer contention: exactly one winner.
        winners: list[str] = []
        losers: list[str] = []
        start_gate = asyncio.Event()

        async def contender(gateway_id: str):
            await start_gate.wait()
            try:
                await _claim_once(js, nc, task_id, lane, gateway_id)
                winners.append(gateway_id)
            except LaneContestedError:
                losers.append(gateway_id)
            except Exception as exc:  # pragma: no cover - defensive
                failures.append(f"contender {gateway_id} failed unexpectedly: {exc}")

        contender_tasks = [
            asyncio.create_task(contender("gateway-a")),
            asyncio.create_task(contender("gateway-b")),
        ]
        start_gate.set()
        await asyncio.gather(*contender_tasks)

        contention_ok = len(winners) == 1 and len(losers) == 1 and len(set(winners) & set(losers)) == 0
        passes["dual_claim_single_winner"] = {
            "winners": winners,
            "losers": losers,
            "ok": contention_ok,
        }
        if not contention_ok:
            failures.append("dual-claim did not produce exactly one winner and one loser")

        # 2) stale-lock recovery.
        stale_task = uuid4()
        stale_payload = {
            "gateway_id": "ghost-gateway",
            "task_id": str(task_id),
            "fencing_token": 999,
            "acquired_at": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat(),
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
            "lane_id": lane,
        }
        await lanes_kv.put(lane, json.dumps(stale_payload).encode())
        try:
            await _claim_once(js, nc, stale_task, lane, "gateway-recovery")
            stale_ok = True
            passes["stale_recovery"] = {
                "ok": True,
                "reclaimer": "gateway-recovery",
            }
        except Exception as exc:
            stale_ok = False
            passes["stale_recovery"] = {
                "ok": False,
                "error": str(exc),
            }
            failures.append("stale lock was not reclaimable")

        # 3) TTL renewal keeps ownership.
        renew_task = uuid4()
        renew_lane = f"dual-claim-renew-{lane_uuid}"
        await lanes_kv.delete(renew_lane)
        await fencing_kv.delete(renew_lane)

        async def holder():
            async with LaneLockedExecution(js, await _build_task(renew_task, renew_lane), "gateway-owner", nc):
                await asyncio.sleep(20)

        holder_task = asyncio.create_task(holder())
        await asyncio.sleep(1)

        contender_ok = True
        # 3.1 before TTL expiry + renewal (still running), contender should lose.
        await asyncio.sleep(9)
        try:
            await _claim_once(js, nc, renew_task, renew_lane, "gateway-contender")
            contender_ok = False
            failures.append("contender acquired refreshed lock unexpectedly")
        except LaneContestedError:
            contender_ok = contender_ok and True
        except Exception as exc:
            contender_ok = False
            failures.append(f"contender unexpected error during renewal phase: {exc}")

        # 3.2 later check after same holder should still hold due repeated heartbeat.
        await asyncio.sleep(4)
        try:
            await _claim_once(js, nc, renew_task, renew_lane, "gateway-contender")
            contender_ok = False
            failures.append("contender acquired lock after longer hold despite renewal")
        except LaneContestedError:
            contender_ok = contender_ok and True
        except Exception as exc:
            contender_ok = False
            failures.append(f"contender unexpected error during second renewal check: {exc}")

        holder_task.cancel()
        with suppress(asyncio.CancelledError):
            await holder_task

        passes["ttl_heartbeat_renewal"] = {"ok": contender_ok}

    finally:
        if lanes_kv is not None:
            await lanes_kv.delete(lane)
        if fencing_kv is not None:
            await fencing_kv.delete(lane)
        if nc is not None:
            await nc.drain()
            await nc.close()

    return {
        "ok": not failures,
        "phase": "completed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "nats_url": NATS_URL,
        "passes": passes,
        "failures": failures,
    }


def main() -> int:
    result = asyncio.run(_run_drill())

    artifact_dir = Path(".state")
    artifact_dir.mkdir(exist_ok=True, parents=True)
    artifact_path = artifact_dir / f"lane-lock-contest-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    artifact_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(json.dumps(result, indent=2))
    print(f"Artifact: {artifact_path}")

    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
