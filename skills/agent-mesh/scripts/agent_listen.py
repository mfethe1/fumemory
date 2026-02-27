"""Listen for incoming agent messages via NATS. Run as background process."""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import nats

NATS_URLS = [
    os.environ.get("NATS_LOCAL_URL", "nats://100.76.63.58:4222"),
    os.environ.get("NATS_RAILWAY_URL", "nats://gondola.proxy.rlwy.net:22393"),
]


async def handle_message(msg, nc, agent_name):
    """Process an incoming message and optionally respond."""
    try:
        data = json.loads(msg.data.decode())
        sender = data.get("from", "?")
        msg_type = data.get("type", "?")
        priority = data.get("priority", "normal")
        payload = data.get("payload", {})
        message = payload.get("message", "")
        msg_id = data.get("msg_id", "?")

        prefix = "[URGENT] " if priority == "urgent" else ""
        print(f"\n{prefix}[{msg_type}] from {sender}: {message}")
        print(f"  Subject: {msg.subject} | ID: {msg_id[:8]}")

        # Auto-respond to pings
        if msg_type == "ping":
            pong = {
                "msg_id": msg_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "from": agent_name,
                "to": sender,
                "type": "response",
                "priority": "normal",
                "payload": {
                    "message": f"{agent_name} is online",
                    "reply_to": msg_id,
                },
            }
            await nc.publish(f"swarm.agent.{sender}.response", json.dumps(pong).encode())
            print(f"  Auto-responded: pong to {sender}")

    except Exception as e:
        print(f"  Error processing message: {e}")


async def listen(agent_name: str):
    nc = None
    for url in NATS_URLS:
        try:
            nc = await nats.connect(url, connect_timeout=5)
            print(f"Connected to NATS: {url}")
            break
        except Exception:
            continue

    if not nc:
        print("FATAL: Cannot connect to any NATS node")
        sys.exit(1)

    # Subscribe to our inbox, broadcast, and ping channels
    subjects = [
        f"swarm.agent.{agent_name}.inbox",
        f"swarm.agent.{agent_name}.response",
        "swarm.agent.broadcast",
        "swarm.agent.ping",
    ]

    async def dispatch(msg):
        await handle_message(msg, nc, agent_name)

    for subject in subjects:
        await nc.subscribe(subject, cb=dispatch)
        print(f"Subscribed: {subject}")

    print(f"\n{agent_name} listener active. Waiting for messages...\n")

    # Keep alive
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await nc.drain()


def main():
    parser = argparse.ArgumentParser(description="Listen for incoming agent messages")
    parser.add_argument("--agent", "-a", required=True, help="Your agent name (winnie/lenny/mack/rosie)")
    args = parser.parse_args()
    asyncio.run(listen(args.agent))


if __name__ == "__main__":
    main()
