"""Ping all agents to check who's online."""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import nats

NATS_URLS = [
    url
    for url in (
        os.environ.get("NATS_LOCAL_URL", "nats://127.0.0.1:4222"),
        os.environ.get("NATS_RAILWAY_URL"),
    )
    if url
]

AGENTS = ["winnie", "lenny", "mack", "rosie"]


async def ping_all(from_agent: str = "probe"):
    nc = None
    for url in NATS_URLS:
        try:
            nc = await nats.connect(url, connect_timeout=5)
            break
        except Exception:
            continue

    if not nc:
        print("FATAL: Cannot connect to any NATS node")
        sys.exit(1)

    # Subscribe to our response channel
    responses = {}
    response_subject = f"swarm.agent.{from_agent}.response"

    async def on_response(msg):
        data = json.loads(msg.data.decode())
        responses[data.get("from", "?")] = data

    await nc.subscribe(response_subject, cb=on_response)

    # Broadcast ping
    ping = {
        "msg_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "from": from_agent,
        "to": "all",
        "type": "ping",
        "priority": "normal",
        "payload": {"message": "ping"},
    }

    await nc.publish("swarm.agent.ping", json.dumps(ping).encode())
    print(f"Ping sent to all agents. Waiting 5s for responses...\n")

    await asyncio.sleep(5)

    print("Agent Status:")
    print("-" * 40)
    for agent in AGENTS:
        if agent in responses:
            ts = responses[agent].get("timestamp", "?")
            print(f"  {agent:10s}  ONLINE   ({ts})")
        else:
            print(f"  {agent:10s}  OFFLINE  (no response)")
    print("-" * 40)
    print(f"{len(responses)}/{len(AGENTS)} agents online")

    await nc.drain()
    return len(responses)


if __name__ == "__main__":
    from_agent = os.environ.get("AGENT_NAME", "probe")
    if len(sys.argv) > 1:
        from_agent = sys.argv[1]
    result = asyncio.run(ping_all(from_agent))
    sys.exit(0 if result > 0 else 1)
