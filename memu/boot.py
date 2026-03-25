"""Gateway boot script — stateless container entrypoint.

This runs as the first thing when a Docker container starts.
It reads env vars, connects to the mesh, hydrates state from memU,
downloads the Epistemic Blackboard, and resumes the assigned task.

Environment Variables (injected by the Warden):
    GATEWAY_ID       - Unique identifier for this gateway instance
    GATEWAY_ROLE     - Agent role (e.g., 'winnie', 'rosie', 'lenny', 'mack')
    TASK_ID          - If resuming a specific task (optional)
    NATS_LOCAL_URL   - Primary NATS instance
    NATS_RAILWAY_URL - Fallback NATS instance
    MEMU_API_URL     - memU API endpoint
    MEMU_API_KEY     - memU authentication key
    WARDEN_SECRET    - HMAC secret for signing Warden requests
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from uuid import UUID

logger = logging.getLogger(__name__)

# --- Environment ---

GATEWAY_ID = os.environ.get("GATEWAY_ID", f"gw-{os.getpid()}")
GATEWAY_ROLE = os.environ.get("GATEWAY_ROLE", "generic")
TASK_ID = os.environ.get("TASK_ID")
NATS_LOCAL_URL = os.environ.get("NATS_LOCAL_URL", "nats://localhost:4222")
NATS_RAILWAY_URL = os.environ.get("NATS_RAILWAY_URL")
if not NATS_RAILWAY_URL:
    raise RuntimeError("NATS_RAILWAY_URL environment variable is required")
MEMU_API_URL = os.environ.get("MEMU_API_URL", "http://localhost:8000")
MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")
MAX_HYDRATION_TOKENS = int(os.environ.get("MAX_HYDRATION_TOKENS", "8000"))


async def hydrate_state() -> dict:
    """Pull context from memU for task resumption.

    1. Download Epistemic Blackboard (shared knowledge)
    2. If TASK_ID set, pull the latest checkpoint scratchpad
    3. Pull the relevant DAG subgraph for context
    4. Enforce MAX_HYDRATION_TOKENS budget
    5. If over budget, run summarization pass

    Returns a dict with the hydrated context ready for LLM injection.
    """
    import httpx

    context = {
        "gateway_id": GATEWAY_ID,
        "role": GATEWAY_ROLE,
        "blackboard": [],
        "checkpoint": None,
        "dag_context": None,
        "hydration_prompt": None,
        "boot_time": datetime.now(timezone.utc).isoformat(),
    }

    headers = {"X-API-Key": MEMU_API_KEY}

    async with httpx.AsyncClient(base_url=MEMU_API_URL, timeout=10.0) as client:
        # 1. Download Blackboard
        try:
            # Search for blackboard entries in memU
            r = await client.post(
                "/search",
                headers=headers,
                json={
                    "query": "epistemic blackboard shared knowledge",
                    "limit": 50,
                    "memory_type": "fact",
                },
            )
            if r.status_code == 200:
                results = r.json()
                context["blackboard"] = [
                    {"content": m["memory"]["content"], "confidence": m["memory"]["confidence"]}
                    for m in results[:20]
                ]
                logger.info(f"Hydrated {len(context['blackboard'])} blackboard entries")
        except Exception as e:
            logger.warning(f"Blackboard hydration failed: {e}")

        # 2. Pull checkpoint if resuming a task
        if TASK_ID:
            try:
                r = await client.post(
                    "/search",
                    headers=headers,
                    json={
                        "query": f"checkpoint scratchpad task {TASK_ID}",
                        "limit": 1,
                    },
                )
                if r.status_code == 200:
                    results = r.json()
                    if results:
                        checkpoint_content = results[0]["memory"]["content"]

                        # Token budget check
                        estimated_tokens = len(checkpoint_content) // 4
                        if estimated_tokens > MAX_HYDRATION_TOKENS:
                            logger.info(
                                f"Checkpoint ({estimated_tokens} tokens) exceeds budget "
                                f"({MAX_HYDRATION_TOKENS}). Summarizing..."
                            )
                            checkpoint_content = await _summarize(
                                client, checkpoint_content, MAX_HYDRATION_TOKENS
                            )

                        context["checkpoint"] = checkpoint_content
                        context["hydration_prompt"] = (
                            "You are recovering a task from a failed node. "
                            "Here is their partial reasoning. Validate their logic "
                            "and complete the task. Do not restart from scratch."
                        )
                        logger.info(f"Hydrated checkpoint for task {TASK_ID}")

            except Exception as e:
                logger.warning(f"Checkpoint hydration failed: {e}")

    return context


async def _summarize(client, text: str, max_tokens: int) -> str:
    """Summarize text to fit within token budget using a cheap model."""
    try:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Summarize this agent's work-in-progress into a dense, "
                            "actionable checkpoint. Preserve: current step, key decisions, "
                            "partial results, and next actions. Be concise."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                "max_tokens": max_tokens,
            },
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"Summarization failed, truncating: {e}")

    # Fallback: hard truncate
    return text[: max_tokens * 4] + "\n\n[TRUNCATED — original checkpoint exceeded token budget]"


async def register_with_mesh():
    """Connect to NATS and announce this gateway's capabilities."""
    import nats

    from memu.cluster import NATSClusterManager
    from memu.swarm_models import GatewayAnnounce, GatewayStatus

    cluster = NATSClusterManager(
        local_url=NATS_LOCAL_URL,
        railway_url=NATS_RAILWAY_URL,
    )
    await cluster.connect()

    # === SUICIDE SIGNAL LISTENER (Lenny's Split-Brain Fix) ===
    # High-priority listener: if this gateway's ID appears on the suicide channel,
    # it means the Warden has revoked our fencing token and spawned a replacement.
    # We are a "ghost" — exit immediately before corrupting any state.
    nc = cluster.active_connection

    async def _suicide_handler(msg):
        """Hard exit on suicide signal — no cleanup, no DB writes, just die."""
        try:
            data = json.loads(msg.data.decode())
            target = data.get("target_gateway_id", "")
            if target == GATEWAY_ID:
                logger.critical(
                    f"☠ SUICIDE SIGNAL received — I am a ghost gateway. "
                    f"Reason: {data.get('reason', 'unknown')}. Exiting immediately."
                )
                sys.exit(0)
        except Exception:
            pass

    # Subscribe to both the specific and wildcard suicide channels
    await nc.subscribe(f"swarm.advisory.suicide.{GATEWAY_ID}", cb=_suicide_handler)
    await nc.subscribe("swarm.advisory.suicide.*", cb=_suicide_handler)
    logger.info(f"Suicide signal listener active on swarm.advisory.suicide.{GATEWAY_ID}")

    # Announce presence
    announce = GatewayAnnounce(
        gateway_id=GATEWAY_ID,
        capabilities=[],  # Populated by the gateway's tool registry
        status=GatewayStatus.ONLINE,
        metadata={
            "role": GATEWAY_ROLE,
            "boot_time": datetime.now(timezone.utc).isoformat(),
            "task_id": TASK_ID,
        },
    )

    await nc.publish(
        "swarm.discovery",
        announce.model_dump_json().encode(),
    )

    logger.info(f"Gateway {GATEWAY_ID} ({GATEWAY_ROLE}) registered with mesh")
    return cluster


async def start_heartbeat(cluster, interval_s: float = 3.0):
    """Publish heartbeat every interval_s to prove we're alive."""
    nc = cluster.active_connection

    while True:
        try:
            await nc.publish(
                "swarm.warden.heartbeat",
                json.dumps({
                    "gateway_id": GATEWAY_ID,
                    "task_id": TASK_ID,
                    "progress_pct": 0.0,
                    "status_note": "running",
                }).encode(),
            )
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(1)



# --- Swarm execution path (smoke mode) ---

async def _run_smoke_task(cluster, task_id: str):
    """Run a minimal lane-locked task loop so Glass Box sees lane claim + heartbeats."""
    from uuid import UUID

    from memu.lane_lock import DuplicateClaimError, LaneLockedExecution, LaneContestedError
    from memu.swarm_models import EventType, TaskNode, TaskStatus

    task_uuid = UUID(task_id)
    task = TaskNode(
        task_id=task_uuid,
        root_prompt_id=task_uuid,
        title=f"smoke-task-{task_id[:8]}",
        description="Lenny smoke task for Warden/ledger visibility",
        status=TaskStatus.CLAIMED,
        assigned_gateway=GATEWAY_ID,
        resource_lanes=[f"lane-smoke-{task_id}"],
        rollback_instruction="noop",
        compute_budget=0.0,
    )

    nc = cluster.active_connection
    js = cluster.jetstream

    while True:
        try:
            async with LaneLockedExecution(js, task, GATEWAY_ID, nc) as executor:
                # Claim is only emitted after lock is held to avoid split-brain duplicates.
                await executor.publish_event(EventType.TASK_CLAIMED, {
                    "gateway_id": GATEWAY_ID,
                    "role": GATEWAY_ROLE,
                    "task_type": "smoke",
                })

                # Keep lane lock alive so Warden can see heartbeat windows and lane contention.
                while True:
                    await asyncio.sleep(2)
        except (LaneContestedError, DuplicateClaimError) as e:
            # If lane is contested or duplicate, retry after backoff and keep container alive.
            logger.warning(
                "Lane lock contention while entering smoke task (%s), retrying", e
            )
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(
                "Execution error while running smoke task (%s), retrying", e
            )
            await asyncio.sleep(2)


_boot_time = time.time()


async def main():
    """Container entrypoint — hydrate, register, start heartbeat, run agent."""
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{GATEWAY_ID}] %(levelname)s %(message)s",
    )

    logger.info(f"=== Gateway Boot ===")
    logger.info(f"  ID:   {GATEWAY_ID}")
    logger.info(f"  Role: {GATEWAY_ROLE}")
    logger.info(f"  Task: {TASK_ID or 'none (warm standby)'}")

    # Step 1: Hydrate state from memU (with hard timeout — Failure Mode 4)
    from memu.hardening import hydrate_with_timeout
    context = await hydrate_with_timeout(hydrate_state)
    logger.info(f"  Hydration complete: {len(context['blackboard'])} blackboard entries")
    if context.get("hydration_failed"):
        logger.warning(f"  ⚠ Hydration failed: {context.get('failure_reason')}. Running with empty context.")
    elif context["checkpoint"]:
        logger.info(f"  Checkpoint recovered — resuming task {TASK_ID}")

    # Step 2: Connect to NATS mesh
    try:
        cluster = await register_with_mesh()
    except Exception as e:
        logger.error(f"Failed to connect to mesh: {e}")
        logger.info("Running in standalone mode (no mesh)")
        cluster = None

    # Step 3: Start heartbeat only for task-bearing runtime
    heartbeat_task = None
    if cluster and TASK_ID:
        heartbeat_task = asyncio.create_task(start_heartbeat(cluster))

    # Step 4: Install graceful shutdown handlers (Failure Mode: SIGTERM)
    from memu.hardening import install_signal_handlers, register_shutdown_handler

    async def _graceful_shutdown():
        logger.info("Graceful shutdown: releasing resources...")
        if heartbeat_task:
            heartbeat_task.cancel()
        if cluster:
            # Publish draining status before exit
            try:
                nc = cluster.active_connection
                await nc.publish(
                    "swarm.discovery",
                    json.dumps({
                        "gateway_id": GATEWAY_ID,
                        "status": "offline",
                        "reason": "graceful_shutdown",
                    }).encode(),
                )
            except Exception:
                pass
            await cluster.close()

    register_shutdown_handler(_graceful_shutdown)
    try:
        install_signal_handlers(asyncio.get_event_loop())
    except Exception:
        pass  # Signal handlers not available on all platforms

    # Step 5: Run the actual agent logic
    # If TASK_ID is set, run a deterministic lane-locked smoke task and emit heartbeat stream.
    # Otherwise, keep container available as warm standby (no NATS heartbeat).
    try:
        if TASK_ID:
            logger.info("Starting smoke task execution for %s", TASK_ID)
            try:
                await _run_smoke_task(cluster, TASK_ID)
            except Exception as e:
                logger.exception("Smoke task execution failed: %s", e)
        else:
            logger.info("Gateway ready. Warm standby mode: waiting for task assignment...")
            while True:
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await _graceful_shutdown()
        logger.info("Gateway shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
