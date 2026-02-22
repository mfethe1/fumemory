"""Send a message directly to another agent via NATS."""
import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import nats

NATS_URLS = [
    os.environ.get("NATS_LOCAL_URL", "nats://100.76.63.58:4222"),
    os.environ.get("NATS_RAILWAY_URL", "nats://gondola.proxy.rlwy.net:22393"),
]

VALID_AGENTS = ["winnie", "lenny", "mack", "rosie", "coordinator"]
VALID_TYPES = ["request", "response", "task", "status", "broadcast", "ping"]


async def send(to: str, msg_type: str, message: str, from_agent: str, priority: str = "normal", reply_to: str = None, wait_response: bool = False):
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

    msg_id = str(uuid.uuid4())
    envelope = {
        "msg_id": msg_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "from": from_agent,
        "to": to,
        "type": msg_type,
        "priority": priority,
        "payload": {
            "message": message,
            "reply_to": reply_to,
        },
    }

    if msg_type == "broadcast":
        subject = "swarm.agent.broadcast"
    elif msg_type == "ping":
        subject = "swarm.agent.ping"
    else:
        subject = f"swarm.agent.{to}.inbox"

    await nc.publish(subject, json.dumps(envelope).encode())
    print(f"Sent [{msg_type}] to {to}: {message[:80]}")
    print(f"  Subject: {subject}")
    print(f"  ID: {msg_id}")

    if wait_response:
        response_subject = f"swarm.agent.{from_agent}.response"
        sub = await nc.subscribe(response_subject)
        print(f"  Waiting for response on {response_subject}...")
        try:
            resp = await asyncio.wait_for(sub.next_msg(), timeout=30)
            data = json.loads(resp.data.decode())
            print(f"  Response from {data.get('from', '?')}: {data['payload'].get('message', '')[:200]}")
        except asyncio.TimeoutError:
            print("  No response after 30s")
        await sub.unsubscribe()

    await nc.drain()


def main():
    parser = argparse.ArgumentParser(description="Send a message to another agent via NATS")
    parser.add_argument("--to", required=True, choices=VALID_AGENTS, help="Target agent")
    parser.add_argument("--type", default="request", choices=VALID_TYPES, help="Message type")
    parser.add_argument("--message", "-m", required=True, help="Message content")
    parser.add_argument("--from", dest="from_agent", default=None, help="Your agent name (auto-detected if not set)")
    parser.add_argument("--priority", default="normal", choices=["low", "normal", "urgent"])
    parser.add_argument("--reply-to", default=None, help="msg_id this is replying to")
    parser.add_argument("--wait", action="store_true", help="Wait for response (30s timeout)")

    args = parser.parse_args()

    # Auto-detect agent name from environment
    from_agent = args.from_agent
    if not from_agent:
        from_agent = os.environ.get("AGENT_NAME", os.environ.get("GATEWAY_ID", "unknown"))

    asyncio.run(send(
        to=args.to,
        msg_type=args.type,
        message=args.message,
        from_agent=from_agent,
        priority=args.priority,
        reply_to=args.reply_to,
        wait_response=args.wait,
    ))


if __name__ == "__main__":
    main()
