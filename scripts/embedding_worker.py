"""Dedicated embedding worker — runs as a separate Railway service.

This is the ONLY process that loads ML model weights into RAM.
Gateway pods use memu/embeddings_client.py to request embeddings via NATS RPC.

Usage:
    python -m scripts.embedding_worker

Railway deployment:
    - Separate service with its own Dockerfile
    - Needs 2GB+ RAM for model weights
    - Uses persistent volume at /data/cache for model caching
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

import nats
from memu.nats_config import primary_nats_url

logger = logging.getLogger(__name__)

EMBED_SUBJECT = "memu.rpc.embed"


async def run() -> None:
    model = None  # Lazy-load on first request

    nc = await nats.connect(primary_nats_url())
    logger.info("Embedding worker connected to NATS")

    async def handle_request(msg) -> None:
        nonlocal model
        try:
            if model is None:
                from fastembed import TextEmbedding

                model = TextEmbedding()
                logger.info("FastEmbed model loaded")

            payload = json.loads(msg.data.decode())
            text = payload.get("text", "")
            if not text:
                await msg.respond(json.dumps({"error": "empty text"}).encode())
                return

            embeddings = list(model.embed([text]))
            vector = embeddings[0].tolist()
            await msg.respond(json.dumps({"embedding": vector}).encode())
        except Exception as exc:
            logger.error("Embedding failed: %s", exc)
            await msg.respond(json.dumps({"error": str(exc)}).encode())

    # Use queue group so multiple embedding workers load-balance
    await nc.subscribe(EMBED_SUBJECT, cb=handle_request, queue="embed-workers")
    logger.info("Listening on %s (queue=embed-workers)", EMBED_SUBJECT)

    stop = asyncio.Event()

    def _signal_handler() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        loop.add_signal_handler(signal.SIGINT, _signal_handler)
    except NotImplementedError:
        # Windows fallback
        pass

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logger.info("Embedding worker shutting down")
        await nc.drain()
        await nc.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())

