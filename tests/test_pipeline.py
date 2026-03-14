"""Manual smoke helper for sending events through the pipeline.

This file lives under tests/ for convenience/history, but it is not an automated
pytest test and must never run network side effects at import time.
"""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import nats


NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")


async def send_test_events():
    nc = await nats.connect(NATS_URL)

    root_id = str(uuid.uuid4())
    task1_id = str(uuid.uuid4())
    task2_id = str(uuid.uuid4())
    task3_id = str(uuid.uuid4())

    events = [
        {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_gateway": "winnie",
            "event_type": "task_drafted",
            "task_id": task1_id,
            "payload": {
                "title": "Deploy NATS to Railway",
                "root_prompt_id": root_id,
                "parent_task_id": None,
                "compute_budget": 0.50,
            },
            "compute_cost": 0.001,
        },
        {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_gateway": "lenny",
            "event_type": "task_drafted",
            "task_id": task2_id,
            "payload": {
                "title": "Wire Warden Engine",
                "root_prompt_id": root_id,
                "parent_task_id": task1_id,
                "compute_budget": 1.00,
            },
            "compute_cost": 0.001,
        },
        {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_gateway": "winnie",
            "event_type": "task_drafted",
            "task_id": task3_id,
            "payload": {
                "title": "Glass Box E2E Test",
                "root_prompt_id": root_id,
                "parent_task_id": task1_id,
                "compute_budget": 0.25,
            },
            "compute_cost": 0.001,
        },
        {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_gateway": "mack",
            "event_type": "bid_submitted",
            "task_id": task1_id,
            "payload": {"gateway_id": "mack"},
            "compute_cost": 0.0,
        },
        {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_gateway": "coordinator",
            "event_type": "lease_granted",
            "task_id": task1_id,
            "payload": {"gateway_id": "mack"},
            "compute_cost": 0.0,
        },
        {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_gateway": "mack",
            "event_type": "task_claimed",
            "task_id": task1_id,
            "payload": {"gateway_id": "mack"},
            "compute_cost": 0.01,
        },
        {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_gateway": "mack",
            "event_type": "task_completed",
            "task_id": task1_id,
            "payload": {"result": "NATS deployed successfully"},
            "compute_cost": 0.05,
        },
    ]

    for event in events:
        await nc.publish("swarm.events.live", json.dumps(event).encode())
        title = event["payload"].get("title", event["task_id"][:8])
        print(f"Published: {event['event_type']} -> {title}")
        await asyncio.sleep(0.3)

    await nc.drain()
    print(f"\nSent {len(events)} events to {NATS_URL}. Open http://localhost:3001 to see the Glass Box!")


if __name__ == "__main__":
    asyncio.run(send_test_events())
