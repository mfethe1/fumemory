#!/usr/bin/env python3
"""Initialize NATS JetStream streams and KV buckets for the Predictive Intent System.

Creates the infrastructure agreed upon in deliberation rounds 1-2:
  - 2 JetStream streams (PREDICTION_EVENTS, SYSTEM_EVENTS)
  - 10 KV buckets for distributed state

Run once after NATS is up. Safe to re-run (idempotent).
"""

from __future__ import annotations

import asyncio
import os
import logging

import nats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")


async def main():
    nc = await nats.connect(NATS_URL)
    js = nc.jetstream()
    logger.info(f"Connected to NATS at {NATS_URL}")

    # ── JetStream Streams ──
    streams = [
        {
            "name": "PREDICTION_EVENTS",
            "subjects": ["predict.outcome.*", "predict.made.*", "predict.feedback.*", "predict.gate.*"],
            "max_age": 30 * 24 * 3600,  # 30 days in seconds
            "max_msgs": 50_000,
        },
        {
            "name": "SYSTEM_EVENTS",
            "subjects": [
                "system.deploy.*", "system.cron.*", "system.git.*",
                "system.mount.*", "system.error.*", "system.session.*",
            ],
            "max_age": 30 * 24 * 3600,
            "max_msgs": 100_000,
        },
    ]

    for s in streams:
        try:
            name = await js.find_stream_name_by_subject(s["subjects"][0])
            logger.info(f"Stream '{name}' already exists (covers {s['subjects'][0]})")
        except Exception:
            await js.add_stream(
                name=s["name"],
                subjects=s["subjects"],
                max_age=s["max_age"],
                max_msgs=s["max_msgs"],
            )
            logger.info(f"Created stream: {s['name']} ({s['subjects']})")

    # ── KV Buckets ──
    # TTL in seconds for nats.py
    HOUR = 3600
    MINUTE = 60

    buckets = [
        {"bucket": "PREDICTION_ACCURACY", "ttl": 0},
        {"bucket": "PREDICTION_WEIGHTS", "ttl": 0},
        {"bucket": "PREDICTION_CACHE", "ttl": 5 * MINUTE},
        {"bucket": "EVENT_CORRELATIONS", "ttl": 0},
        {"bucket": "SESSION_STATE", "ttl": 4 * HOUR},
        {"bucket": "MARKOV_TRANSITIONS", "ttl": 0},
        {"bucket": "HINT_STATE", "ttl": 24 * HOUR},
        {"bucket": "ACTIVE_CHAINS", "ttl": 2 * HOUR},
        {"bucket": "PREDICTION_DASHBOARD", "ttl": 10 * MINUTE},
        {"bucket": "CACHE_SHADOW_STATS", "ttl": 0},
    ]

    for b in buckets:
        try:
            kv = await js.key_value(b["bucket"])
            logger.info(f"KV bucket '{b['bucket']}' already exists")
        except Exception:
            kwargs = {"bucket": b["bucket"]}
            if b["ttl"]:
                kwargs["ttl"] = b["ttl"]
            await js.create_key_value(**kwargs)
            logger.info(f"Created KV bucket: {b['bucket']} (TTL: {b['ttl'] if b['ttl'] else 'none'}s)")

    logger.info("All prediction infrastructure created successfully ✅")

    # Verify
    logger.info("\n--- Verification ---")
    for s in streams:
        try:
            name = await js.find_stream_name_by_subject(s["subjects"][0])
            info = await js.stream_info(name)
            logger.info(f"  ✅ Stream {info.config.name}: {info.state.messages} msgs")
        except Exception as e:
            logger.error(f"  ❌ Stream {s['name']}: {e}")

    for b in buckets:
        try:
            kv = await js.key_value(b["bucket"])
            status = await kv.status()
            logger.info(f"  ✅ KV {b['bucket']}: {status.values} entries")
        except Exception as e:
            logger.error(f"  ❌ KV {b['bucket']}: {e}")

    await nc.close()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
