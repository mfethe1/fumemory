from __future__ import annotations

from memu.nats_config import primary_nats_url, resolve_nats_endpoints


def test_primary_nats_url_prefers_local_then_railway_then_default(monkeypatch):
    monkeypatch.delenv("NATS_URL", raising=False)
    monkeypatch.setenv("NATS_LOCAL_URL", "nats://local:4222")
    monkeypatch.setenv("NATS_RAILWAY_URL", "nats://railway:4222")
    assert primary_nats_url() == "nats://local:4222"

    monkeypatch.delenv("NATS_LOCAL_URL", raising=False)
    assert primary_nats_url() == "nats://railway:4222"

    monkeypatch.delenv("NATS_RAILWAY_URL", raising=False)
    assert primary_nats_url() == "nats://localhost:4222"


def test_legacy_nats_url_still_backfills_local_endpoint(monkeypatch):
    monkeypatch.delenv("NATS_LOCAL_URL", raising=False)
    monkeypatch.delenv("NATS_RAILWAY_URL", raising=False)
    monkeypatch.setenv("NATS_URL", "nats://legacy:4222")

    local, railway = resolve_nats_endpoints()

    assert local == "nats://legacy:4222"
    assert railway is None
