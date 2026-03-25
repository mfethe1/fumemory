from __future__ import annotations

import pytest

from memu.gateway_federation import (
    FederationConfig,
    allowed_subject,
    build_dispatch_envelope,
    build_gateway_announce,
    durable_consumer_name,
    response_subject,
    validate_gateway_id,
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


def test_federation_config_validation_requires_nats_url() -> None:
    cfg = FederationConfig(gateway_id="mac-mini-main", nats_railway_url="nats://railway:4222")
    cfg.validate()
    with pytest.raises(ValueError):
        FederationConfig(gateway_id="mac-mini-main", nats_railway_url="").validate()
