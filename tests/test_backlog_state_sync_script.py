from __future__ import annotations

from scripts.sync_backlog_state_events import build_backlog_synced_events


def test_build_backlog_synced_events_shapes_expected_payload():
    md = """
## BuildBid Hardening Sprint
- **Task:** OAuth/auth reliability + end-to-end sign-in green (OWNER: Macklemore)
  - STATUS: in_progress
  - NEXT ACTION: run live Google OAuth smoke matrix
  - BLOCKER: missing OAuth creds
""".strip()

    events = build_backlog_synced_events(backlog_text=md)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "backlog.task.synced"
    assert event["entity_id"].startswith("oauth-auth-reliability")
    assert event["payload"]["owner"] == "Macklemore"
    assert event["payload"]["status"] == "in_progress"
    assert event["payload"]["next_action"] == "run live Google OAuth smoke matrix"
    assert event["payload"]["blocker"] == "missing OAuth creds"
    assert event["payload"]["source_file"] == "BACKLOG.md"


def test_build_backlog_synced_events_event_id_is_stable_for_same_task_state():
    md = """
- **Task:** Create backlog_items + backlog_events schema (OWNER: Macklemore)
  - STATUS: in_progress
  - NEXT ACTION: emit backlog.task.synced events
""".strip()

    first = build_backlog_synced_events(backlog_text=md)[0]
    second = build_backlog_synced_events(backlog_text=md)[0]

    assert first["event_id"] == second["event_id"]
