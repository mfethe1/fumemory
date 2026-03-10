from __future__ import annotations

import os
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from memu.tenancy import DEFAULT_TENANT_ID, resolve_tenant_id

MEMU_API_KEY = os.environ.get("MEMU_API_KEY", "memu-dev-key")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


class AuthContext(str):
    """Authenticated request context with tenant information.

    Extends str for backward compatibility — existing endpoints that
    type-hint `_key: str` still work. Access tenant_id via .tenant_id.
    """

    tenant_id: UUID

    def __new__(cls, api_key: str, tenant_id: UUID | None = None):
        instance = super().__new__(cls, api_key)
        instance.tenant_id = tenant_id or DEFAULT_TENANT_ID
        return instance


async def verify_api_key(
    x_api_key: Annotated[str | None, Security(api_key_header)] = None,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)] = None,
) -> AuthContext:
    """Accept X-API-Key as canonical auth with Bearer as a compatibility alias."""

    provided_key = x_api_key or (bearer.credentials if bearer else None)
    if not provided_key or provided_key != MEMU_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        from memu import api as memu_api  # local import avoids circular import at module load

        pool = getattr(memu_api, "pool", None)
    except Exception:
        pool = None

    tenant_id = await resolve_tenant_id(pool, provided_key) if pool else DEFAULT_TENANT_ID
    return AuthContext(api_key=provided_key, tenant_id=tenant_id)
