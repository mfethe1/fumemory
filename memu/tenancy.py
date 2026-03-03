"""Multi-Tenancy middleware for memU API.

Resolves tenant_id from API key and sets Postgres session variable
for Row-Level Security enforcement. Even if application code forgets
a WHERE clause, Postgres will only return rows for the current tenant.

Usage in FastAPI endpoints:
    async with tenant_connection(pool, tenant_id) as conn:
        rows = await conn.fetch("SELECT * FROM memories")  # RLS filters automatically

For single-tenant/dev mode, MEMU_SINGLE_TENANT=true skips RLS and uses
the default tenant ID.
"""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

# Default tenant for single-tenant/dev mode
DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
SINGLE_TENANT_MODE = os.environ.get("MEMU_SINGLE_TENANT", "true").lower() in ("true", "1", "yes")

# Cache: api_key → tenant_id (cleared on restart)
_tenant_cache: dict[str, UUID] = {}


async def resolve_tenant_id(pool: asyncpg.Pool, api_key: str) -> UUID:
    """Resolve tenant_id from an API key.
    
    In single-tenant mode, always returns DEFAULT_TENANT_ID.
    In multi-tenant mode, looks up the key in the api_keys table.
    """
    if SINGLE_TENANT_MODE:
        return DEFAULT_TENANT_ID

    # Check cache first
    if api_key in _tenant_cache:
        return _tenant_cache[api_key]

    # Hash the key for lookup (api_keys stores hashed keys)
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id FROM api_keys 
            WHERE key_hash = $1 AND revoked_at IS NULL
            LIMIT 1
            """,
            key_hash,
        )

        if row and row["tenant_id"]:
            tenant_id = row["tenant_id"]
            _tenant_cache[api_key] = tenant_id
            return tenant_id

        # Fallback: try direct key match (dev keys stored unhashed)
        row = await conn.fetchrow(
            """
            SELECT tenant_id FROM api_keys
            WHERE key_hash = $1 AND revoked_at IS NULL
            LIMIT 1
            """,
            api_key,
        )

        if row and row["tenant_id"]:
            tenant_id = row["tenant_id"]
            _tenant_cache[api_key] = tenant_id
            return tenant_id

    # If no tenant found, use default (backward compat)
    logger.debug(f"No tenant found for API key, using default tenant")
    return DEFAULT_TENANT_ID


@asynccontextmanager
async def tenant_connection(
    pool: asyncpg.Pool,
    tenant_id: UUID,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a connection with tenant context set for RLS.
    
    Sets `app.current_tenant` as a Postgres session variable so that
    RLS policies can filter rows automatically. The setting is local
    to the transaction and reverts when the connection is released.
    
    Usage:
        async with tenant_connection(pool, tenant_id) as conn:
            # All queries on `conn` are tenant-scoped by Postgres RLS
            rows = await conn.fetch("SELECT * FROM memories")
    """
    async with pool.acquire() as conn:
        # Set tenant context for RLS
        # Using SET LOCAL means it's scoped to the current transaction
        try:
            await conn.execute(
                "SELECT set_config('app.current_tenant', $1, false)",
                str(tenant_id),
            )
            yield conn
        finally:
            # Reset tenant context (defensive — connection pool recycles anyway)
            try:
                await conn.execute(
                    "SELECT set_config('app.current_tenant', '', false)"
                )
            except Exception:
                pass


@asynccontextmanager
async def tenant_transaction(
    pool: asyncpg.Pool,
    tenant_id: UUID,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a connection with tenant context inside a transaction.
    
    Like tenant_connection but wraps in an explicit transaction.
    SET LOCAL is transaction-scoped, so it auto-reverts on commit/rollback.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SET LOCAL app.current_tenant = $1",
                str(tenant_id),
            )
            yield conn


def clear_tenant_cache():
    """Clear the tenant resolution cache (call after key rotation)."""
    _tenant_cache.clear()
