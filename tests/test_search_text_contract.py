from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from memu import api


class _FakeConn:
    async def fetch(self, _query: str, *params):
        # params[0] is the ILIKE query payload ("%query%")
        q = params[0] if params else ""
        if "missing" in str(q):
            return []

        now = datetime.now(timezone.utc)
        return [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "content": "health probe memory",
                "memory_type": "fact",
                "agent_id": "macklemore",
                "metadata": {"source": "contract-test"},
                "parent_id": None,
                "confidence": 0.9,
                "access_count": 1,
                "decay_score": 0.0,
                "created_at": now,
                "updated_at": now,
            }
        ]


class _AcquireCtx:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def acquire(self):
        return _AcquireCtx()


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


def _client() -> TestClient:
    api.app.router.lifespan_context = _noop_lifespan
    api.pool = _FakePool()
    api.MEMU_API_KEY = "test-key"
    return TestClient(api.app)


def test_search_text_auth_no_auth_and_invalid_key_matrix():
    with _client() as client:
        no_auth = client.post("/search-text", params={"query": "health", "limit": 1})
        assert no_auth.status_code == 401

        bad_auth = client.post(
            "/search-text",
            params={"query": "health", "limit": 1},
            headers={"X-API-Key": "wrong-key"},
        )
        assert bad_auth.status_code == 401

        ok = client.post(
            "/search-text",
            params={"query": "health", "limit": 1},
            headers={"X-API-Key": "test-key"},
        )
        assert ok.status_code == 200
        assert len(ok.json()) == 1


def test_search_text_valid_and_invalid_payload_matrix():
    with _client() as client:
        valid = client.post(
            "/search-text",
            params={"query": "health", "limit": 1},
            headers={"X-API-Key": "test-key"},
        )
        assert valid.status_code == 200

        invalid_payload = client.post(
            "/search-text",
            json={"query": "health", "limit": 1},
            headers={"X-API-Key": "test-key"},
        )
        assert invalid_payload.status_code == 422
