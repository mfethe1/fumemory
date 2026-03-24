"""Lightweight NATS RPC client for embedding inference.

Gateway pods use this to offload ML model inference to a dedicated
embedding worker (scripts/embedding_worker.py) via NATS Request-Reply.
This keeps Gateway pods lean (no sentence-transformers / fastembed in RAM).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

EMBED_SUBJECT = "memu.rpc.embed"
EMBED_TIMEOUT_S = 5.0


async def get_embedding_via_rpc(nc: Any, text: str) -> list[float] | None:
    """Request an embedding vector from the dedicated embedding worker via NATS Request-Reply.

    Falls back to None if the worker is unavailable or times out.
    The Gateway pod does NOT load any ML models — all inference
    is offloaded to the embedding worker (scripts/embedding_worker.py).
    """
    try:
        payload = json.dumps({"text": text}).encode()
        response = await nc.request(EMBED_SUBJECT, payload, timeout=EMBED_TIMEOUT_S)
        result = json.loads(response.data.decode())
        embedding = result.get("embedding")
        if embedding and isinstance(embedding, list):
            return embedding
        logger.warning("Embedding RPC returned invalid response: %s", result.get("error"))
        return None
    except Exception as exc:
        logger.warning("Embedding RPC failed (timeout or no responders): %s", exc)
        return None

