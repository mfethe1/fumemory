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
from collections import Counter
from datetime import datetime, timezone

import nats

from memu.nats_config import resolve_nats_endpoints

logger = logging.getLogger(__name__)

NATS_LOCAL_URL, NATS_RAILWAY_URL = resolve_nats_endpoints()
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


def _record_event(subject_counts: Counter[str], type_counts: Counter[str], payload: dict | str, subject: str) -> None:
    subject_counts[subject] += 1
    if not isinstance(payload, dict):
        return
    event_type = payload.get("event_type") or payload.get("type") or payload.get("status_note")
    if event_type:
        type_counts[str(event_type)] += 1


def _heartbeat_summary(subject_counts: Counter[str], type_counts: Counter[str], elapsed: float) -> str:
    return (
        "consumer heartbeat: total=%s elapsed=%.1fs distinct_subjects=%s top_subjects=%s top_types=%s"
        % (
            sum(subject_counts.values()),
            elapsed,
            len(subject_counts),
            subject_counts.most_common(10),
            type_counts.most_common(10),
        )
    )


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

            subject_counts: Counter[str] = Counter()
            type_counts: Counter[str] = Counter()
            start = datetime.now(timezone.utc)
            last_summary = datetime.now(timezone.utc)

            subscriptions = [await nc.subscribe(subject) for subject in SUBJECTS]

            async def _iter_messages():
                while True:
                    pending = [asyncio.create_task(sub.messages.__anext__()) for sub in subscriptions]
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    for task in done:
                        yield task.result()

            async for msg in _iter_messages():
                now = datetime.now(timezone.utc)
                payload = _safe_decode(msg.data)
                _record_event(subject_counts, type_counts, payload, msg.subject)

                logger.info("EVENT %s subject=%s", now.isoformat(), msg.subject)
                logger.debug("payload=%r", payload)

                if isinstance(payload, dict):
                    task_id = payload.get("task_id") or payload.get("gateway_id") or "-"
                    logger.info(" payload_keys=%s task=%s", list(payload.keys())[:8], task_id)

                if (now - last_summary).total_seconds() >= SUMMARY_INTERVAL_SECONDS:
                    elapsed = (now - start).total_seconds()
                    logger.info(_heartbeat_summary(subject_counts, type_counts, elapsed))
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
