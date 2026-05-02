import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_deployment.py"
spec = importlib.util.spec_from_file_location("verify_deployment", SCRIPT_PATH)
verify_deployment = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(verify_deployment)


def test_request_sends_both_auth_headers(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b"[]"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout=15):
        captured["headers"] = dict(req.header_items())
        return FakeResponse()

    monkeypatch.setattr(verify_deployment.urllib.request, "urlopen", fake_urlopen)
    status, body = verify_deployment._request(
        "GET",
        "http://example.test/health",
        api_key="test-key",
    )

    assert status == 200
    assert body == "[]"
    assert captured["headers"]["X-memu-key"] == "test-key"
    assert captured["headers"]["X-api-key"] == "test-key"


def test_write_sync_memory_uses_supported_memory_type(monkeypatch):
    captured = []

    def fake_request(method, url, *, api_key=None, json_body=None, timeout=15):
        captured.append((url, json_body))
        return 200, "{}"

    monkeypatch.setattr(verify_deployment, "_request", fake_request)

    content = verify_deployment._write_sync_memory("http://example.test", "test-key")

    assert content is not None
    assert captured[0][0].endswith("/upsert")
    assert captured[0][1]["memory_type"] == "fact"


def test_check_recall_accepts_json_list(monkeypatch):
    def fake_request(method, url, *, api_key=None, json_body=None, timeout=15):
        return 200, "[]"

    monkeypatch.setattr(verify_deployment, "_request", fake_request)

    assert verify_deployment._check_recall("http://example.test", "test-key", "hello") is True


def test_check_recall_tolerates_missing_route(monkeypatch):
    def fake_request(method, url, *, api_key=None, json_body=None, timeout=15):
        return 404, '{"detail":"Not Found"}'

    monkeypatch.setattr(verify_deployment, "_request", fake_request)

    assert verify_deployment._check_recall("http://example.test", "test-key", "hello") is True
