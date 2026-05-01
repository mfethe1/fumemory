import json

from scripts import verify_deployment as vd


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_verify_search_text_success(monkeypatch):
    def fake_urlopen(req, timeout=15):
        assert req.full_url == "http://memu.test/search-text?q=needle&limit=1"
        assert req.headers["X-memu-key"] == "secret"
        return _FakeResponse(200, json.dumps([{"content": "needle in a haystack"}]))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    assert vd._verify_search_text("http://memu.test", "secret", "needle") is True


def test_verify_search_recall_success(monkeypatch):
    def fake_urlopen(req, timeout=15):
        assert req.full_url == "http://memu.test/search/recall?query=needle&limit=3"
        assert req.headers["X-memu-key"] == "secret"
        return _FakeResponse(200, json.dumps({"results": [{"content": "needle in a haystack"}]}))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    assert vd._verify_search_recall("http://memu.test", "secret", "needle") is True


def test_verify_search_recall_rejects_unexpected_payload(monkeypatch):
    def fake_urlopen(req, timeout=15):
        return _FakeResponse(200, json.dumps({"ok": True}))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    assert vd._verify_search_recall("http://memu.test", "secret", "needle") is False


def test_endpoint_uses_compat_suffix_for_production_api_base():
    assert vd._endpoint("https://api.example.com/api/v1/memu", "/memories", "/add") == "https://api.example.com/api/v1/memu/add"
    assert vd._endpoint("http://localhost:8000", "/memories", "/add") == "http://localhost:8000/memories"


def test_api_candidates_auto_prioritizes_local_then_production(monkeypatch):
    monkeypatch.setattr(vd, "_default_api_url", lambda: "http://127.0.0.1:8000")
    assert vd._api_candidates("auto") == [
        "http://127.0.0.1:8000",
        "https://api-production-86f5.up.railway.app/api/v1/memu",
    ]


def test_api_candidates_explicit_url_disables_fallback():
    assert vd._api_candidates("http://memu.test") == ["http://memu.test"]
