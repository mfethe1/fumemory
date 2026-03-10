from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memu.bridge_ledger import BridgeLedger


def test_bridge_ledger_tracks_gateway_subjects_and_effective_status():
    ledger = BridgeLedger()
    now = datetime.now(timezone.utc)

    ledger.process_event(
        {
            "_subject": "swarm.gateway.register",
            "gateway_id": "mack",
            "status": "online",
            "metadata": {"role": "infra"},
            "timestamp": (now - timedelta(seconds=5)).isoformat(),
        }
    )
    ledger.process_event(
        {
            "_subject": "swarm.gateway.heartbeat",
            "gateway_id": "mack",
            "status": "online",
            "current_task_id": "task-42",
            "active_nats_node": "local",
            "timestamp": (now - timedelta(seconds=5)).isoformat(),
        }
    )
    ledger.process_event(
        {
            "_subject": "swarm.gateway.heartbeat",
            "gateway_id": "winnie",
            "status": "online",
            "timestamp": (now - timedelta(seconds=25)).isoformat(),
        }
    )

    status = ledger.get_gateway_status(stale_after_s=20, offline_after_s=60)
    gateways = {item["gateway_id"]: item for item in status["gateways"]}

    assert gateways["mack"]["effective_status"] == "online"
    assert gateways["mack"]["current_task_id"] == "task-42"
    assert gateways["mack"]["active_nats_node"] == "local"
    assert gateways["winnie"]["effective_status"] == "degraded"
    assert status["summary"]["stale_gateways"] == ["winnie"]
