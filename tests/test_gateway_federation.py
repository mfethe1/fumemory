from __future__ import annotations

import pytest

from memu.gateway_federation import (
    FederationConfig,
    SmokeResult,
    allowed_subject,
    build_dispatch_envelope,
    build_gateway_announce,
    build_memu_smoke_write_payload,
    durable_consumer_name,
    response_subject,
    results_to_json,
    validate_gateway_id,
    verify_memu_replay_response,
)


def test_validate_gateway_id_accepts_stable_slug() -> None:
    validate_gateway_id("mac-mini-main")


@pytest.mark.parametrize("gateway_id", ["", "Mac Mini", "rosie_mini", "bad.gateway"])
def test_validate_gateway_id_rejects_invalid_values(gateway_id: str) -> None:
    with pytest.raises(ValueError):
        validate_gateway_id(gateway_id)


def test_durable_consumer_name_is_stable_and_uppercase() -> None:
    assert durable_consumer_name("mac-mini-main") == "MEMU_MAC_MINI_MAIN_AGENT_EVENTS"
    assert (
        durable_consumer_name("rosie-mini", stream="swarm.events")
        == "MEMU_ROSIE_MINI_SWARM_EVENTS"
    )


def test_allowed_subject_respects_self_scoped_response_channel() -> None:
    gateway_id = "mac-mini-main"
    assert allowed_subject("swarm.discovery", gateway_id) is True
    assert allowed_subject(response_subject(gateway_id), gateway_id) is True
    assert allowed_subject("swarm.rpc.response.other-gateway", gateway_id) is False
    assert allowed_subject("swarm.events.*", gateway_id) is False


def test_build_gateway_announce_matches_contract() -> None:
    payload = build_gateway_announce("mac-mini-main", capabilities=["memu", "swarm"])
    assert payload["gateway_id"] == "mac-mini-main"
    assert payload["transport"]["railway_nats"] is True
    assert payload["transport"]["agent_events"] is True


def test_build_dispatch_envelope_contains_required_fields() -> None:
    payload = build_dispatch_envelope(
        source_gateway_id="mac-mini-main",
        target_gateway_id="rosie-mini",
        title="Run QA sweep",
        prompt="Run a synthetic QA sweep",
        capabilities=["qa"],
    )
    assert payload["version"] == 1
    assert payload["source_gateway_id"] == "mac-mini-main"
    assert payload["target_gateway_id"] == "rosie-mini"
    assert payload["kind"] == "task.dispatch"
    assert payload["ttl_seconds"] == 300
    assert payload["payload"]["budget"]["max_agents"] == 3
    assert payload["idempotency_key"].startswith(payload["root_task_id"])
    assert payload["idempotency_key"].endswith("run-qa-sweep")


def test_memu_smoke_write_payload_uses_evidence_memory_for_idempotency() -> None:
    cfg = FederationConfig(gateway_id="mac-mini-main", nats_railway_url="nats://railway:4222")

    payload = build_memu_smoke_write_payload(cfg, marker="smoke-123")

    assert payload["memory_kind"] == "evidence"
    assert payload["idempotency_key"] == "gateway-smoke-mac-mini-main-smoke-123"
    assert payload["metadata"]["source"] == "gateway-federation-smoke"


def test_memu_replay_accepts_same_id_200() -> None:
    ok, detail = verify_memu_replay_response(
        200,
        {"id": "mem-123"},
        original_id="mem-123",
    )

    assert ok is True
    assert "same id" in detail


def test_memu_replay_accepts_legacy_409() -> None:
    ok, detail = verify_memu_replay_response(
        409,
        {"error_code": "IDEMPOTENCY_CONFLICT"},
        original_id="mem-123",
    )

    assert ok is True
    assert "409" in detail


def test_memu_replay_rejects_different_200_id() -> None:
    ok, detail = verify_memu_replay_response(
        200,
        {"id": "different"},
        original_id="mem-123",
    )

    assert ok is False
    assert "different" in detail


def test_federation_config_validation_requires_nats_url() -> None:
    cfg = FederationConfig(gateway_id="mac-mini-main", nats_railway_url="nats://railway:4222")
    cfg.validate()
    with pytest.raises(ValueError):
        FederationConfig(gateway_id="mac-mini-main", nats_railway_url="").validate()


# --- results_to_json proof format ---

def test_results_to_json_includes_gateway_and_nats_fields() -> None:
    results = [SmokeResult(name="config", ok=True, detail="ok")]
    out = results_to_json(results, gateway_id="test-gw", nats_railway_url="nats://token@host:4222")
    assert out["gateway_id"] == "test-gw"
    assert "[REDACTED]" in out["nats_railway_url"]
    assert "token" not in out["nats_railway_url"]
    assert "evidence_memory_ids" in out


def test_results_to_json_redacts_nats_url_with_credentials() -> None:
    results = [SmokeResult(name="config", ok=True, detail="ok")]
    out = results_to_json(results, gateway_id="test-gw", nats_railway_url="nats://secret@railway-host:4222")
    assert out["nats_railway_url"] == "nats://[REDACTED]@railway-host:4222"
    assert "secret" not in out["nats_railway_url"]


def test_results_to_json_keeps_nats_url_without_credentials() -> None:
    results = [SmokeResult(name="config", ok=True, detail="ok")]
    out = results_to_json(results, gateway_id="test-gw", nats_railway_url="nats://railway-host:4222")
    assert out["nats_railway_url"] == "nats://railway-host:4222"


def test_results_to_json_extracts_evidence_memory_ids() -> None:
    results = [
        SmokeResult(name="memu-write", ok=True, detail="ok", data={"id": "mem-abc-123", "content": "smoke"}),
    ]
    out = results_to_json(results, gateway_id="test-gw")
    assert "mem-abc-123" in out["evidence_memory_ids"]


def test_results_to_json_excludes_memory_id_from_failed_write() -> None:
    results = [
        SmokeResult(name="memu-write", ok=False, detail="write status=500", data=None),
    ]
    out = results_to_json(results, gateway_id="test-gw")
    assert out["evidence_memory_ids"] == []


def test_results_to_json_overall_false_when_any_check_fails() -> None:
    results = [
        SmokeResult(name="config", ok=True, detail="ok"),
        SmokeResult(name="connect", ok=False, detail="connection refused"),
    ]
    out = results_to_json(results, gateway_id="test-gw")
    assert out["ok"] is False
