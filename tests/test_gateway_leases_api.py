from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from memu.api import acquire_gateway_lease, release_gateway_lease, renew_gateway_lease
from memu.models import (
    GatewayLeaseAcquireRequest,
    GatewayLeaseReleaseRequest,
    GatewayLeaseRenewRequest,
)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def transaction(self):
        return FakeTransaction()

    async def fetchrow(self, query, *params):
        self.calls.append((query, params))
        if self.rows:
            return self.rows.pop(0)
        return None


def lease_row(owner_gateway: str = "winnie", *, expired: bool = False, version: int = 1):
    now = datetime.now(timezone.utc)
    return {
        "lease_key": "telegram:-100/topic:242",
        "owner_gateway": owner_gateway,
        "backup_gateway": "lenny",
        "lease_expires_at": now - timedelta(seconds=5) if expired else now + timedelta(seconds=120),
        "last_message_id": "100",
        "last_reply_id": None,
        "context_digest": "digest",
        "task_state": {"step": "triage"},
        "metadata": {"channel": "telegram"},
        "version": version,
        "created_at": now,
        "updated_at": now,
    }


@asynccontextmanager
async def fake_tenant_conn(_auth, conn):
    yield conn


@pytest.mark.asyncio
async def test_acquire_gateway_lease_creates_new_lease():
    conn = FakeConn([None, lease_row("winnie", version=1)])
    req = GatewayLeaseAcquireRequest(
        lease_key="telegram:-100/topic:242",
        gateway_id="winnie",
        backup_gateway="lenny",
        ttl_seconds=120,
    )

    with patch("memu.api._tenant_conn", side_effect=lambda auth: fake_tenant_conn(auth, conn)), \
         patch("memu.api._publish_gateway_lease_event", new=AsyncMock()):
        res = await acquire_gateway_lease(req, _key="test")

    assert res.status == "claimed"
    assert res.lease.owner_gateway == "winnie"
    assert res.lease.lease_key == req.lease_key


@pytest.mark.asyncio
async def test_acquire_gateway_lease_rejects_active_foreign_owner():
    conn = FakeConn([lease_row("rosie", expired=False, version=3)])
    req = GatewayLeaseAcquireRequest(
        lease_key="telegram:-100/topic:242",
        gateway_id="winnie",
        ttl_seconds=120,
    )

    with patch("memu.api._tenant_conn", side_effect=lambda auth: fake_tenant_conn(auth, conn)), \
         patch("memu.api._publish_gateway_lease_event", new=AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await acquire_gateway_lease(req, _key="test")

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_acquire_gateway_lease_steals_expired_lease():
    conn = FakeConn([lease_row("rosie", expired=True, version=3), lease_row("winnie", expired=False, version=4)])
    req = GatewayLeaseAcquireRequest(
        lease_key="telegram:-100/topic:242",
        gateway_id="winnie",
        backup_gateway="lenny",
        ttl_seconds=120,
    )

    with patch("memu.api._tenant_conn", side_effect=lambda auth: fake_tenant_conn(auth, conn)), \
         patch("memu.api._publish_gateway_lease_event", new=AsyncMock()):
        res = await acquire_gateway_lease(req, _key="test")

    assert res.status == "stolen"
    assert res.lease.owner_gateway == "winnie"
    assert res.lease.version == 4


@pytest.mark.asyncio
async def test_renew_gateway_lease_requires_current_owner():
    conn = FakeConn([None])
    req = GatewayLeaseRenewRequest(
        lease_key="telegram:-100/topic:242",
        gateway_id="winnie",
        ttl_seconds=120,
    )

    with patch("memu.api._tenant_conn", side_effect=lambda auth: fake_tenant_conn(auth, conn)), \
         patch("memu.api._publish_gateway_lease_event", new=AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await renew_gateway_lease(req, _key="test")

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_release_gateway_lease_requires_current_owner():
    conn = FakeConn([None])
    req = GatewayLeaseReleaseRequest(
        lease_key="telegram:-100/topic:242",
        gateway_id="winnie",
    )

    with patch("memu.api._tenant_conn", side_effect=lambda auth: fake_tenant_conn(auth, conn)), \
         patch("memu.api._publish_gateway_lease_event", new=AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await release_gateway_lease(req, _key="test")

    assert exc.value.status_code == 409
