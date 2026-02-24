#!/usr/bin/env python3
"""Initialize NATS KV buckets for shared state."""

import asyncio
import os
import nats
from nats.js.kv import KeyValue

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")

async def main():
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()

    buckets = [
        ("GATEWAY_STATE", "Live operational state of all gateways"),
        ("GLOBAL_SETTINGS", "Shared team-wide settings and feature flags"),
        ("RESOURCE_LANES", "Semantic mutex locks for resource lanes"),
        ("FENCING_TOKENS", "Monotonically increasing fencing tokens per lane"),
    ]

    for bucket, description in buckets:
        try:
            kv = await js.key_value(bucket)
            print(f"Bucket {bucket} already exists.")
        except Exception:
            await js.create_key_value(bucket=bucket, description=description)
            print(f"Created bucket {bucket}.")

    await nc.close()

if __name__ == "__main__":
    asyncio.run(main())
