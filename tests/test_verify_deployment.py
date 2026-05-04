import json
import os

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
    ok, detail = vd._verify_search_text("http://memu.test", "secret", "needle")
    assert ok is True


def test_verify_search_recall_success(monkeypatch):
    def fake_urlopen(req, timeout=15):
        assert req.full_url == "http://memu.test/search/recall?query=needle&limit=3"
        assert req.headers["X-memu-key"] == "secret"
        return _FakeResponse(200, json.dumps({"results": [{"content": "needle in a haystack"}]}))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    ok, detail = vd._verify_search_recall("http://memu.test", "secret", "needle")
    assert ok is True


def test_verify_search_recall_rejects_unexpected_payload(monkeypatch):
    def fake_urlopen(req, timeout=15):
        return _FakeResponse(200, json.dumps({"ok": True}))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    ok, detail = vd._verify_search_recall("http://memu.test", "secret", "needle")
    assert ok is False


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


# --- Core readiness returns (bool, str) tuples ---

def test_check_health_returns_tuple(monkeypatch):
    monkeypatch.setattr(vd.urllib.request, "urlopen", lambda req, timeout=15: _FakeResponse(200, '{"status":"healthy"}'))
    ok, detail = vd._check_health("http://memu.test")
    assert ok is True
    assert "200" in detail


def test_check_health_returns_false_on_503(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=15):
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    class _Err503:
        def read(self):
            return b"db down"

    def fake_urlopen2(req, timeout=15):
        err = urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)
        err.read = lambda: b"db down"
        raise err

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen2)
    ok, detail = vd._check_health("http://memu.test")
    assert ok is False
    assert "503" in detail


# --- Federation gate ---

def test_check_federation_passes_on_write_then_409_then_search(monkeypatch):
    """Federation gate: write succeeds, replay returns 409, recall finds the memory."""
    call_log = []

    content_holder: list[str] = []

    def fake_urlopen(req, timeout=15):
        url = req.full_url
        call_log.append(url)

        # First POST /memories → 200 (idempotency write)
        if "/memories" in url and len([u for u in call_log if "/memories" in u]) == 1:
            body = json.dumps({"id": "abc-123", "content": "Federation Readiness Proof"})
            return _FakeResponse(200, body)

        # Second POST /memories → 409 (idempotency replay dedup proof)
        if "/memories" in url and len([u for u in call_log if "/memories" in u]) == 2:
            body = json.dumps({"detail": "An Evidence Memory with this idempotency_key already exists"})
            err = vd.urllib.error.HTTPError(url, 409, "Conflict", {}, None)
            err.read = lambda: body.encode("utf-8")
            raise err

        # GET /search/recall → 200 with the written memory
        if "/search/recall" in url:
            body = json.dumps({"results": [{"content": "Federation Readiness Proof verify-federation-1234567890"}]})
            return _FakeResponse(200, body)

        return _FakeResponse(200, json.dumps([]))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vd.time, "time", lambda: 1234567890.0)

    ok, checks = vd._check_federation("http://memu.test", "secret")

    assert ok is True
    names = [c["name"] for c in checks]
    assert "idempotency_write" in names
    assert "idempotency_replay" in names
    assert "federation_searchable_proof" in names

    write_check = next(c for c in checks if c["name"] == "idempotency_write")
    assert write_check["passed"] is True

    replay_check = next(c for c in checks if c["name"] == "idempotency_replay")
    assert replay_check["passed"] is True
    assert "409" in replay_check["detail"]

    search_check = next(c for c in checks if c["name"] == "federation_searchable_proof")
    assert search_check["passed"] is True


def test_check_federation_fails_when_write_fails(monkeypatch):
    """Federation gate stops immediately if the idempotency write fails."""
    def fake_urlopen(req, timeout=15):
        err = vd.urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, None)
        err.read = lambda: b"server error"
        raise err

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)

    ok, checks = vd._check_federation("http://memu.test", "secret")

    assert ok is False
    write_check = next(c for c in checks if c["name"] == "idempotency_write")
    assert write_check["passed"] is False


def test_check_federation_fails_when_replay_not_deduplicated(monkeypatch):
    """Federation gate fails if replay returns 200 instead of 409."""
    call_count = [0]

    def fake_urlopen(req, timeout=15):
        call_count[0] += 1
        if "/memories" in req.full_url:
            # Both writes return 200 — no dedup
            return _FakeResponse(200, json.dumps({"id": "abc-123", "content": "proof"}))
        if "/search/recall" in req.full_url:
            return _FakeResponse(200, json.dumps({"results": [{"content": "proof"}]}))
        return _FakeResponse(200, json.dumps([]))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)

    ok, checks = vd._check_federation("http://memu.test", "secret")

    assert ok is False
    replay_check = next(c for c in checks if c["name"] == "idempotency_replay")
    assert replay_check["passed"] is False
    assert "200" in replay_check["detail"]


def test_core_readiness_does_not_call_federation(monkeypatch):
    """Core gate does not exercise federation checks when --check-federation is absent."""
    federation_called = [False]
    original = vd._check_federation

    def spy(*args, **kwargs):
        federation_called[0] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(vd, "_check_federation", spy)

    calls = []

    def fake_urlopen(req, timeout=15):
        calls.append(req.full_url)
        if "/health" in req.full_url:
            return _FakeResponse(200, '{"status":"healthy"}')
        if "/memories" in req.full_url:
            return _FakeResponse(200, json.dumps({"id": "x", "content": "probe"}))
        if "/search-text" in req.full_url:
            return _FakeResponse(200, json.dumps([{"content": "probe"}]))
        if "/search/recall" in req.full_url:
            return _FakeResponse(200, json.dumps({"results": [{"content": "probe"}]}))
        return _FakeResponse(200, json.dumps([]))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vd.time, "time", lambda: 1.0)
    monkeypatch.setattr(vd, "_write_sync_memory", lambda url, key: ("probe", "ok"))

    result = vd._verify_single(
        "http://memu.test", "secret",
        check_federation=False,
        check_async=False,
        proof_out=None,
    )

    assert result is True
    assert federation_called[0] is False


# --- Proof artifact ---

def test_build_proof_redacts_api_key():
    proof = vd._build_proof(
        gate="core",
        api_url="http://memu.test",
        api_key="super-secret-key",
        checks=[{"name": "health", "passed": True, "detail": "status=200"}],
        overall=True,
    )
    assert proof["api_key"] == "[REDACTED]"
    assert "super-secret-key" not in json.dumps(proof)


def test_build_proof_structure():
    checks = [
        {"name": "health", "passed": True, "detail": "status=200"},
        {"name": "canonical_write", "passed": True, "detail": "ok"},
    ]
    proof = vd._build_proof(
        gate="federation",
        api_url="http://memu.test",
        api_key="key",
        checks=checks,
        overall=True,
    )
    assert proof["schema_version"] == 1
    assert proof["gate"] == "federation"
    assert proof["api_url"] == "http://memu.test"
    assert proof["overall"] == "pass"
    assert proof["checks"] == checks
    assert "timestamp_utc" in proof


def test_build_proof_overall_fail():
    proof = vd._build_proof(
        gate="core",
        api_url="http://memu.test",
        api_key="key",
        checks=[{"name": "health", "passed": False, "detail": "status=503"}],
        overall=False,
    )
    assert proof["overall"] == "fail"


def test_write_proof_writes_valid_json(tmp_path):
    proof = vd._build_proof(
        gate="core",
        api_url="http://memu.test",
        api_key="key",
        checks=[{"name": "health", "passed": True, "detail": "ok"}],
        overall=True,
    )
    out_path = str(tmp_path / "proof.json")
    vd._write_proof(proof, out_path)
    assert os.path.exists(out_path)
    loaded = json.loads(open(out_path).read())
    assert loaded["gate"] == "core"
    assert loaded["overall"] == "pass"
    assert loaded["api_key"] == "[REDACTED]"


def test_optional_services_absent_does_not_block_core(monkeypatch):
    """Core readiness gate succeeds even when NATS and Temporal env vars are absent."""
    monkeypatch.delenv("NATS_RAILWAY_URL", raising=False)
    monkeypatch.delenv("TEMPORAL_HOST", raising=False)

    def fake_urlopen(req, timeout=15):
        if "/health" in req.full_url:
            return _FakeResponse(200, '{"status":"healthy"}')
        if "/memories" in req.full_url:
            return _FakeResponse(200, json.dumps({"id": "x", "content": "core-probe"}))
        if "/search-text" in req.full_url:
            return _FakeResponse(200, json.dumps([{"content": "core-probe"}]))
        if "/search/recall" in req.full_url:
            return _FakeResponse(200, json.dumps({"results": [{"content": "core-probe"}]}))
        return _FakeResponse(200, json.dumps([]))

    monkeypatch.setattr(vd.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(vd, "_write_sync_memory", lambda url, key: ("core-probe", "ok"))

    result = vd._verify_single(
        "http://memu.test", "secret",
        check_federation=False,
        check_async=False,
        proof_out=None,
    )
    assert result is True
