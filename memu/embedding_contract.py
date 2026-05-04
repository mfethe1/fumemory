"""Embedding contract for fumemory.

Provides alias resolution for EMBEDDING_API_BASE (canonical) with
EMBEDDING_BASE_URL as a deprecated compatibility alias, and schema
verification that the configured EMBEDDING_DIMS matches the active
vector column in the database.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel so we only log the alias warning once per process
_alias_warned = False


def resolve_embedding_api_base() -> str:
    """Return the canonical embedding API base URL.

    Reads EMBEDDING_API_BASE first. If unset, falls back to
    EMBEDDING_BASE_URL with a one-time deprecation warning.
    Returns the OpenAI default when neither is set.
    """
    global _alias_warned
    canonical = os.environ.get("EMBEDDING_API_BASE", "")
    if canonical:
        return canonical

    alias = os.environ.get("EMBEDDING_BASE_URL", "")
    if alias:
        if not _alias_warned:
            logger.warning(
                "EMBEDDING_BASE_URL is a deprecated alias for EMBEDDING_API_BASE. "
                "Update your deployment configuration to use EMBEDDING_API_BASE instead."
            )
            _alias_warned = True
        return alias

    return "https://api.openai.com"


async def verify_embedding_schema(pool: Any, configured_dims: int) -> bool:
    """Check that memories.embedding column dimension matches configured_dims.

    Returns True when the schema matches. Returns False and logs ERROR on
    mismatch or when the column cannot be inspected. A False return signals
    degraded semantic recall until schema and configuration are brought into
    agreement.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT format_type(atttypid, atttypmod) AS col_type
                FROM pg_attribute
                WHERE attrelid = 'public.memories'::regclass
                  AND attname = 'embedding'
                  AND attnum > 0
                  AND NOT attisdropped
                """
            )
    except Exception as exc:
        logger.error(
            "Embedding schema verification failed (could not query pg_attribute): %s. "
            "Semantic recall may be degraded.",
            exc,
        )
        return False

    if row is None:
        logger.error(
            "Embedding schema mismatch: memories.embedding column not found "
            "(EMBEDDING_DIMS=%d). Semantic recall will be degraded.",
            configured_dims,
        )
        return False

    col_type = row["col_type"]  # e.g. "vector(1536)"
    if not col_type:
        logger.error(
            "Could not determine embedding column type. Semantic recall may be degraded."
        )
        return False

    match = re.match(r"vector\((\d+)\)", col_type)
    if not match:
        # Column exists but type is not vector(N) — skip hard failure
        logger.warning(
            "Unexpected embedding column type %r — skipping dimension check.", col_type
        )
        return True

    schema_dims = int(match.group(1))
    if schema_dims != configured_dims:
        logger.error(
            "Embedding dimension mismatch: EMBEDDING_DIMS=%d but schema has %s. "
            "Semantic recall will be degraded until the schema is migrated to match "
            "the configured dimension.",
            configured_dims,
            col_type,
        )
        return False

    logger.info(
        "Embedding schema verified: %s matches EMBEDDING_DIMS=%d.",
        col_type,
        configured_dims,
    )
    return True
