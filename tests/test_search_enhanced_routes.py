from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from memu.api import app as memu_app
from memu.search_enhanced import configure, router


async def _fake_verify_api_key(**_kwargs):
    return "ok"


async def _reject_api_key(**_kwargs):
    raise HTTPException(status_code=401, detail="Missing authentication credentials")


async def _fake_get_embedding(_: str):
    return [0.1, 0.2, 0.3]


class _DummyAcquire:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetch(self, *_args, **_kwargs):
        return []


class _DummyPool:
    def acquire(self):
        return _DummyAcquire()


def test_enhanced_routes_registered_on_main_app():
    paths = {route.path for route in memu_app.routes}
    assert "/search/recall" in paths
    assert "/search/hybrid" in paths
    assert "/search/grep" in paths


def test_enhanced_routes_require_auth_before_querying():
    test_app = FastAPI()
    test_app.include_router(router)
    configure(_DummyPool(), _fake_get_embedding, _reject_api_key)

    client = TestClient(test_app)
    response = client.post("/search/recall", json={"query": "heartbeat test", "limit": 1})

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing authentication credentials"


def test_enhanced_routes_work_when_configured():
    test_app = FastAPI()
    test_app.include_router(router)
    configure(_DummyPool(), _fake_get_embedding, _fake_verify_api_key)

    client = TestClient(test_app)
    response = client.post("/search/recall", json={"query": "heartbeat test", "limit": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["method"] == "hybrid-rrf"
    assert body["results"] == []


def test_enhanced_routes_forward_auth_headers_to_verifier():
    captured = {}

    async def _capture_verify_api_key(**kwargs):
        captured.update(kwargs)
        return "ok"

    test_app = FastAPI()
    test_app.include_router(router)
    configure(_DummyPool(), _fake_get_embedding, _capture_verify_api_key)

    client = TestClient(test_app)
    response = client.post(
        "/search/recall",
        json={"query": "heartbeat test", "limit": 1},
        headers={"X-API-Key": "legacy-key", "X-MemU-Key": "memu-key"},
    )

    assert response.status_code == 200
    assert captured == {"memu_key": "memu-key", "legacy_key": "legacy-key"}
