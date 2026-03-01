#!/usr/bin/env python3
"""memU event consumer (Phase-1): consume swarm events and act as health observer.

This is the first concrete JetStream consumer in the stack, producing
structured logs and optional file-backed evidence.

Behavior:
- Prefer local NATS (NATS_LOCAL_URL)
- Fallback to Railways NATS (NATS_RAILWAY_URL)
- Subscribe to wildcard subjects under swarm.*
- Print parsed events continuously and keep lightweight counters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import nats

logger = logging.getLogger(__name__)

NATS_LOCAL_URL = os.environ.get("NATS_LOCAL_URL")
NATS_RAILWAY_URL = os.environ.get("NATS_RAILWAY_URL")
SUBJECTS = [
    "swarm.>",
]

# Keep only relevant subjects while still listening to all in one stream.
if os.environ.get("SWARM_SUBJECTS"):
    SUBJECTS = [s.strip() for s in os.environ["SWARM_SUBJECTS"].split(",") if s.strip()]

SUMMARY_INTERVAL_SECONDS = int(os.environ.get("SUMMARY_INTERVAL_SECONDS", "30"))


def _safe_decode(payload: bytes) -> dict | str:
    try:
        return json.loads(payload.decode())
    except Exception:
        try:
            return payload.decode(errors="replace")
        except Exception:
            return str(payload)


async def _connect_with_fallback():
    last_error = None

    for url in (NATS_LOCAL_URL, NATS_RAILWAY_URL):
        if not url:
            continue
        try:
            nc = await nats.connect(url)
            logger.info("Connected to NATS: %s", url)
            return nc, url
        except Exception as exc:
            last_error = exc
            logger.warning("NATS connect failed (%s): %s", url, exc)
            continue

    raise RuntimeError(f"NATS unavailable on all endpoints (local/railway). Last: {last_error}")


async def run_consumer():
    while True:
        nc = None
        try:
            nc, connected_url = await _connect_with_fallback()
            subject = SUBJECTS[0] if len(SUBJECTS) == 1 else ",".join(SUBJECTS)
            logger.info("Event consumer active on %s", connected_url)
            logger.info("Subjects: %s", subject)

            counters = defaultdict(int)
            start = datetime.now(timezone.utc)
            last_summary = datetime.now(timezone.utc)

            # Prefer wildcard subscribe for now. Keep per-subject summary from parser.
            sub = await nc.subscribe("swarm.>")

            async for msg in sub.messages:
                now = datetime.now(timezone.utc)
                payload = _safe_decode(msg.data)
                counters[msg.subject] += 1
                counters["total"] += 1

                logger.info("EVENT %s subject=%s", now.isoformat(), msg.subject)
                logger.debug("payload=%r", payload)

                if isinstance(payload, dict):
                    event_type = payload.get("event_type") or payload.get("type") or payload.get("status_note")
                    if event_type:
                        counters[f"type:{event_type}"] += 1

                    # compact log for common swarm events
                    if isinstance(payload, dict):
                        task_id = payload.get("task_id") or payload.get("gateway_id") or "-"
                        logger.info(" payload_keys=%s task=%s", list(payload.keys())[:8], task_id)

                # Heartbeat summary timer
                if (now - last_summary).total_seconds() >= SUMMARY_INTERVAL_SECONDS:
                    elapsed = (now - start).total_seconds()
                    logger.info(
                        "consumer heartbeat: total=%s elapsed=%.1fs subjects=%s top=%s",
                        counters.get("total", 0),
                        elapsed,
                        len(counters),
                        sorted(counters.items(), key=lambda kv: kv[1], reverse=True)[:10],
                    )
                    last_summary = now

        except Exception as exc:
            logger.error("consumer error: %s", exc)
            if nc is not None:
                try:
                    await nc.close()
                except Exception:
                    pass
            await asyncio.sleep(3)
            logger.info("retrying NATS consumer connect in 3s")
            continue
        finally:
            if nc is not None:
                try:
                    await nc.close()
                except Exception:
                    pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [memu-consumer] %(levelname)s %(message)s")
    try:
        asyncio.run(run_consumer())
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":
    main()
