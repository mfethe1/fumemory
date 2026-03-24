"""Serverless Embedding API client.

All ML inference is offloaded to an external API (e.g., OpenAI text-embedding-3-small).
No ML models are loaded in the Gateway pod — this eliminates ~2GB of RAM per replica.

Supports any OpenAI-compatible embedding endpoint (OpenAI, VoyageAI, Azure, Ollama, etc.)
via the EMBEDDING_API_BASE environment variable.
"""

from __future__ import annotations

import logging
import os
import random

import httpx

logger = logging.getLogger(__name__)

# Configuration via environment variables
_API_KEY = os.environ.get("OPENAI_API_KEY", "")
_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
_BASE_URL = os.environ.get("EMBEDDING_API_BASE", "https://api.openai.com")

# Retry configuration for HTTP 429 (Rate Limit)
_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0
_MAX_JITTER_S = 0.5


async def get_embedding(text: str) -> list[float] | None:
    """Get embedding vector from an external Serverless Embedding API.

    Uses exponential backoff with jitter on HTTP 429 responses.
    Returns None on failure (non-429 errors, exhausted retries, missing API key).
    """
    if not _API_KEY:
        logger.warning("OPENAI_API_KEY not set — embedding requests will fail")
        return None

    url = f"{_BASE_URL.rstrip('/')}/v1/embeddings"
    headers = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": _MODEL,
        "input": text,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.post(url, json=payload, headers=headers)

                if resp.status_code == 200:
                    data = resp.json()
                    embedding = data["data"][0]["embedding"]
                    return embedding

                if resp.status_code == 429 and attempt < _MAX_RETRIES:
                    delay = _BASE_DELAY_S * (2 ** attempt) + random.uniform(0, _MAX_JITTER_S)
                    logger.warning(
                        "Embedding API rate-limited (429), retry %d/%d in %.1fs",
                        attempt + 1, _MAX_RETRIES, delay,
                    )
                    import asyncio
                    await asyncio.sleep(delay)
                    continue

                logger.warning(
                    "Embedding API error: HTTP %d — %s",
                    resp.status_code, resp.text[:200],
                )
                return None

            except httpx.TimeoutException:
                logger.warning("Embedding API timeout (attempt %d/%d)", attempt + 1, _MAX_RETRIES + 1)
                if attempt < _MAX_RETRIES:
                    continue
                return None
            except Exception as exc:
                logger.warning("Embedding API request failed: %s", exc)
                return None

    return None

