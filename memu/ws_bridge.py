"""WebSocket bridge — NATS mesh → Glass Box UI.

Subscribes to all swarm.* NATS subjects and streams events
to connected WebSocket clients in real-time.

Endpoints:
    GET  /ws/swarm           → WebSocket stream of all events
    POST /api/halt           → God Mode: halt the swarm
    POST /api/amend/{task_id} → Amend a specific task
    GET  /api/cluster/status → NATS cluster health
    GET  /api/dag/{root_id}  → Full DAG snapshot for a root prompt
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel, Field

from memu.cluster import NATSClusterManager
from memu.bridge_ledger import BridgeLedger
from memu.swarm_models import (
    EventType,
    SwarmEvent,
    SystemHalt,
)

logger = logging.getLogger(__name__)

# --- Globals ---

cluster: NATSClusterManager | None = None
ledger: BridgeLedger = BridgeLedger()
connected_clients: set[WebSocket] = set()

# Per-user connection registry for targeted message delivery.
# Maps user_id/agent_id → WebSocket so that user-specific chat messages
# are forwarded only to the pod where that user is connected.
_active_connections: dict[str, WebSocket] = {}


def register_connection(user_id: str, ws: WebSocket) -> None:
    """Register a user-specific WebSocket connection on this pod."""
    _active_connections[user_id] = ws
    logger.info("Registered user connection: %s (%d active)", user_id, len(_active_connections))


def unregister_connection(user_id: str) -> None:
    """Remove a user-specific WebSocket connection from this pod."""
    _active_connections.pop(user_id, None)
    logger.info("Unregistered user connection: %s (%d active)", user_id, len(_active_connections))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start NATS cluster connection on boot, close on shutdown."""
    global cluster
    cluster = NATSClusterManager()

    try:
        await cluster.connect()
    except ConnectionError:
        logger.error("Failed to connect to NATS cluster — UI will show DISCONNECTED")

    # Start NATS → WebSocket bridge
    bridge_task = asyncio.create_task(_nats_to_ws_bridge())

    yield

    bridge_task.cancel()
    if cluster:
        await cluster.close()


app = FastAPI(
    title="fumemory Glass Box",
    description="Real-time swarm visualization and control.",
    version="0.1.0",
    lifespan=lifespan,
)


# --- WebSocket endpoint ---

@app.websocket("/ws/swarm")
async def ws_swarm(ws: WebSocket):
    """Stream swarm events to the Glass Box UI."""
    await ws.accept()
    connected_clients.add(ws)
    logger.info(f"Glass Box client connected ({len(connected_clients)} total)")

    try:
        # Send cluster status on connect
        if cluster:
            await ws.send_json({
                "type": "cluster_status",
                "data": cluster.status(),
            })

        # Keep connection alive, receive commands from UI
        while True:
            data = await ws.receive_json()
            action = data.get("action")

            if action == "halt":
                await _handle_halt(data)
            elif action == "amend":
                await _handle_amend(data)
            elif action == "status":
                if cluster:
                    await ws.send_json({
                        "type": "cluster_status",
                        "data": cluster.status(),
                    })

    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(ws)
        logger.info(f"Glass Box client disconnected ({len(connected_clients)} total)")


# --- REST endpoints ---

class HaltRequest(BaseModel):
    root_prompt_id: str
    reason: str
    override_instruction: str | None = None


class AmendRequest(BaseModel):
    correction: str


@app.post("/api/halt")
async def halt_swarm(req: HaltRequest):
    """God Mode: broadcast SYSTEM_HALT across the mesh."""
    if not cluster:
        raise HTTPException(503, "NATS cluster not connected")

    halt = SystemHalt(
        root_prompt_id=UUID(req.root_prompt_id),
        halt_reason=req.reason,
        override_instruction=req.override_instruction,
        initiated_by="user",
    )

    event = SwarmEvent(
        source_gateway="glass_box_ui",
        event_type=EventType.SYSTEM_HALT,
        task_id=UUID(req.root_prompt_id),  # Use root prompt as task ID for halt
        payload=halt.model_dump(),
    )

    nc = cluster.active_connection
    await nc.publish("swarm.events", event.model_dump_json().encode())

    logger.warning(f"SYSTEM HALT broadcast: {req.reason}")
    return {"halted": True, "reason": req.reason}


@app.post("/api/amend/{task_id}")
async def amend_task(task_id: str, req: AmendRequest):
    """God Mode: amend a specific task in the DAG."""
    if not cluster:
        raise HTTPException(503, "NATS cluster not connected")

    event = SwarmEvent(
        source_gateway="glass_box_ui",
        event_type=EventType.SYSTEM_OVERRIDE,
        task_id=UUID(task_id),
        payload={
            "correction": req.correction,
            "initiated_by": "user",
        },
    )

    nc = cluster.active_connection
    await nc.publish("swarm.events", event.model_dump_json().encode())

    logger.info(f"Task {task_id} amended by user: {req.correction[:100]}")
    return {"amended": True, "task_id": task_id}


@app.get("/api/cluster/status")
async def cluster_status():
    """NATS cluster health for monitoring."""
    if not cluster:
        return {"status": "disconnected", "nodes": {}}
    return cluster.status()


@app.get("/api/dag")
async def get_dag(root_prompt_id: str | None = None):
    """Get the live DAG projection (all tasks or filtered by root prompt)."""
    return ledger.get_dag(root_prompt_id)


@app.get("/api/events")
async def get_events(limit: int = 100, offset: int = 0):
    """Get recent events from the in-memory ledger."""
    events = ledger.get_event_log(limit=limit, offset=offset)
    return {"events": events, "total": len(ledger.events)}


@app.get("/api/trajectory/{task_id}")
async def get_trajectory(task_id: str):
    """Get the full event trajectory for a specific task."""
    result = ledger.get_trajectory(task_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@app.get("/api/stats")
async def get_stats():
    """Aggregate swarm statistics for the Glass Box dashboard."""
    dag = ledger.get_dag()
    stats = dag["stats"]
    stats["cluster"] = cluster.status() if cluster else {"status": "disconnected"}
    stats["connected_clients"] = len(connected_clients)
    return stats


# --- NATS → WebSocket Bridge ---

async def _nats_to_ws_bridge():
    """Subscribe to NATS swarm.* and broadcast to all WebSocket clients."""
    while True:
        try:
            if not cluster or not cluster.active_connection.is_connected:
                await asyncio.sleep(2)
                continue

            nc = cluster.active_connection
            # Fan-out: every gateway pod must receive broadcasts to check local WS connections.
            # Intentionally NO queue group — if we used queue="...", only one pod would
            # receive each message, and users connected to other pods would never see it.
            sub = await nc.subscribe("swarm.>")  # Wildcard: all swarm subjects

            logger.info("NATS → WebSocket bridge active")

            async for msg in sub.messages:
                try:
                    data = json.loads(msg.data.decode())
                    
                    # Always project into in-memory ledger
                    ledger.process_event(data)
                    
                    if not connected_clients:
                        continue

                    ws_msg = {"type": "event", "data": data}

                    # Broadcast to all connected Glass Box clients
                    disconnected = set()
                    for client in connected_clients:
                        try:
                            await client.send_json(ws_msg)
                        except Exception:
                            disconnected.add(client)

                    connected_clients.difference_update(disconnected)

                    # Per-user targeted delivery: if the message contains a
                    # user_id or agent_id, forward directly to that user's
                    # WebSocket (if connected to THIS pod). Silently ignored
                    # if the user is connected to a different pod.
                    target_id = data.get("user_id") or data.get("agent_id")
                    if target_id and target_id in _active_connections:
                        try:
                            await _active_connections[target_id].send_json(ws_msg)
                        except Exception:
                            unregister_connection(target_id)

                except json.JSONDecodeError:
                    logger.debug(f"Non-JSON message on {msg.subject}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"NATS bridge error: {e}")
            await asyncio.sleep(2)


# --- God Mode Handlers ---

async def _handle_halt(data: dict[str, Any]):
    """Process halt command from WebSocket client."""
    try:
        req = HaltRequest(
            root_prompt_id=data["root_prompt_id"],
            reason=data["reason"],
            override_instruction=data.get("override_instruction"),
        )
        result = await halt_swarm(req)

        # Echo back to all clients
        for client in connected_clients:
            try:
                await client.send_json({"type": "halt_ack", "data": result})
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Halt handler error: {e}")


async def _handle_amend(data: dict[str, Any]):
    """Process amend command from WebSocket client."""
    try:
        task_id = data["task_id"]
        req = AmendRequest(correction=data["correction"])
        await amend_task(task_id, req)
    except Exception as e:
        logger.error(f"Amend handler error: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
