import httpx
import pytest
from unittest.mock import AsyncMock

from memu import api


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ReadyConn:
    async def fetchval(self, query, *args):
        if "SELECT 1" in query:
            return 1
        raise AssertionError(f"unexpected fetchval query: {query}")

    async def fetchrow(self, query, *args):
        if "schema_migrations" in query:
            return {"failed_count": 0, "applied_count": 3}
        raise AssertionError(f"unexpected fetchrow query: {query}")


class _ReadyPool:
    def __init__(self, conn=None):
        self.conn = conn or _ReadyConn()

    def acquire(self):
        return _Acquire(self.conn)


class _ExplodingNats:
    @property
    def active_connection(self):
        raise AssertionError("health must not inspect NATS")


class _ClosablePool:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_api_state(monkeypatch):
    monkeypatch.setattr(api, "pool", None)
    monkeypatch.setattr(api, "is_shutting_down", False)
    monkeypatch.setattr(api, "_embedding_schema_ok", True)
    monkeypatch.setattr(api, "_migrations_ok", True, raising=False)
    monkeypatch.setattr(api, "_migration_startup_error", None, raising=False)
    monkeypatch.setattr(api, "_nats_cluster", None)
    monkeypatch.setattr(api, "_metrics", api._new_metrics(), raising=False)
    monkeypatch.delenv("MEMU_ENABLE_STARTUP_NATS", raising=False)


@pytest.mark.asyncio
async def test_ready_reports_core_api_checks_without_requiring_nats():
    api.pool = _ReadyPool()

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root_response = await client.get("/ready")
        compat_response = await client.get("/api/v1/memu/ready")

    assert root_response.status_code == 200
    assert compat_response.status_code == 200
    assert root_response.json() == compat_response.json()

    payload = root_response.json()
    assert payload["status"] == "ready"
    assert payload["checks"]["database"]["ok"] is True
    assert payload["checks"]["migrations"]["ok"] is True
    assert payload["checks"]["migrations"]["applied_count"] == 3
    assert payload["checks"]["embedding_schema"]["ok"] is True
    assert "nats_startup" not in payload["checks"]


@pytest.mark.asyncio
async def test_health_is_liveness_only_and_does_not_check_startup_nats(monkeypatch):
    monkeypatch.setenv("MEMU_ENABLE_STARTUP_NATS", "1")
    api.pool = _ReadyPool()
    api._nats_cluster = _ExplodingNats()

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_ready_fails_when_startup_nats_is_required_but_unavailable(monkeypatch):
    monkeypatch.setenv("MEMU_ENABLE_STARTUP_NATS", "1")
    api.pool = _ReadyPool()
    api._nats_cluster = None

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["checks"]["database"]["ok"] is True
    assert payload["checks"]["nats_startup"]["required"] is True
    assert payload["checks"]["nats_startup"]["ok"] is False


@pytest.mark.asyncio
async def test_metrics_exposes_request_and_readiness_failure_counters():
    api.pool = _ReadyPool()
    api._embedding_schema_ok = False

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ready_response = await client.get("/ready")
        metrics_response = await client.get("/metrics")

    assert ready_response.status_code == 503
    assert metrics_response.status_code == 200
    assert metrics_response.headers["content-type"].startswith("text/plain")

    body = metrics_response.text
    assert "memu_http_requests_total 1" in body
    assert 'memu_http_requests_by_status_class_total{status_class="5xx"} 1' in body
    assert "memu_http_request_errors_total 1" in body
    assert "memu_http_request_latency_seconds_sum " in body
    assert "memu_http_request_latency_seconds_count 1" in body
    assert "memu_readiness_failures_total 1" in body


@pytest.mark.asyncio
async def test_lifespan_propagates_migration_startup_failures(monkeypatch):
    fake_pool = _ClosablePool()
    migration_error = RuntimeError("migration failed")

    monkeypatch.setattr(api.asyncpg, "create_pool", AsyncMock(return_value=fake_pool))
    monkeypatch.setattr(api, "run_migrations", AsyncMock(side_effect=migration_error))

    with pytest.raises(RuntimeError, match="migration failed"):
        async with api.lifespan(api.app):
            pass

    assert fake_pool.closed is True
    assert api.pool is None
    assert api._migrations_ok is False
    assert api._migration_startup_error == "migration failed"
