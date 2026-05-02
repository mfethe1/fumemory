from __future__ import annotations

import json
from pathlib import Path

from scripts import memu_smoke


class _FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_resolve_api_key_prefers_env(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text('export MEMU_API_KEY="from-env-file"\n')
    monkeypatch.setattr(memu_smoke, "KEY_FILES", (env_path,))
    monkeypatch.setenv("MEMU_API_KEY", "from-env")

    assert memu_smoke.resolve_api_key() == "from-env"


def test_resolve_api_key_reads_dotenv(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("export MEMU_API_KEY='from-dotenv'\n")
    monkeypatch.setattr(memu_smoke, "KEY_FILES", (env_path,))
    monkeypatch.delenv("MEMU_API_KEY", raising=False)

    assert memu_smoke.resolve_api_key() == "from-dotenv"


def test_run_smoke_falls_back_to_search_text(monkeypatch):
    monkeypatch.setattr(memu_smoke, "resolve_api_key", lambda: "secret")

    calls = []

    def fake_request(url, *, method="GET", headers=None, body=None):
        calls.append((url, method, headers, body))
        if url.endswith("/health"):
            return 200, {"status": "healthy"}
        if url.endswith("/search/recall"):
            return 401, {"detail": "Invalid X-MemU-Key"}
        if "/search-text?" in url:
            return 200, [{"id": "1", "content": "heartbeat test"}]
        raise AssertionError(url)

    monkeypatch.setattr(memu_smoke, "_request_json", fake_request)

    result = memu_smoke.run_smoke("http://localhost:8000")

    assert result["ok"] is True
    assert result["search_recall"]["status"] == 401
    assert result["search_text"]["status"] == 200
    assert any("/search-text?query=heartbeat%20test&limit=1" in call[0] for call in calls)


def test_run_smoke_returns_failure_when_health_fails(monkeypatch):
    monkeypatch.setattr(memu_smoke, "resolve_api_key", lambda: None)

    def fake_request(url, *, method="GET", headers=None, body=None):
        if url.endswith("/health"):
            return 503, {"detail": "db down"}
        if url.endswith("/search/recall"):
            return 401, {"detail": "Missing authentication credentials"}
        raise AssertionError(url)

    monkeypatch.setattr(memu_smoke, "_request_json", fake_request)

    result = memu_smoke.run_smoke("http://localhost:8000")

    assert result["ok"] is False
    assert result["health"]["status"] == 503
    assert result["search_text"]["status"] is None
