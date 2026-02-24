#!/usr/bin/env python3
"""Drift Detector — Verifies synchronization across gateways."""

import asyncio
import os
import nats
import json
from datetime import datetime, timezone

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")

async def main():
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    # Get stream info for swarm.events
    try:
        si = await js.stream_info("swarm-events") # Assuming stream name
        latest_seq = si.state.last_seq
    except Exception:
        latest_seq = 0

    # Get state from gateways in KV
    kv = await js.key_value("GATEWAY_STATE")
    gateways = {}
    
    # Iterate all keys in bucket
    try:
        keys = await kv.keys()
        for key in keys:
            entry = await kv.get(key)
            gateways[key] = json.loads(entry.value.decode())
    except Exception:
        pass

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "latest_bus_sequence": latest_seq,
        "gateways": gateways,
    }

    print(json.dumps(report, indent=2))
    
    # Basic drift check: if any gateway hasn't pulsed in > 5 mins, it's drifting/offline
    now = datetime.now(timezone.utc)
    for gw, state in gateways.items():
        lp = datetime.fromisoformat(state["last_pulse"])
        if (now - lp).total_seconds() > 300:
            print(f"ALERT: Gateway {gw} might be drifting or offline (last pulse: {lp})")

    await nc.close()

if __name__ == "__main__":
    asyncio.run(main())
