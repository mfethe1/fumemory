from __future__ import annotations

from collections import Counter

from memu.event_consumer import _heartbeat_summary, _record_event


def test_record_event_tracks_subjects_and_types_separately():
    subject_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()

    _record_event(subject_counts, type_counts, {"event_type": "memory_written", "task_id": "t-1"}, "swarm.memory.written")
    _record_event(subject_counts, type_counts, {"type": "task_completed"}, "swarm.task.completed")
    _record_event(subject_counts, type_counts, "raw-text", "swarm.raw")

    assert subject_counts == Counter(
        {
            "swarm.memory.written": 1,
            "swarm.task.completed": 1,
            "swarm.raw": 1,
        }
    )
    assert type_counts == Counter({"memory_written": 1, "task_completed": 1})


def test_heartbeat_summary_reports_truthful_subject_count():
    summary = _heartbeat_summary(
        Counter({"swarm.memory.written": 2, "swarm.task.completed": 1}),
        Counter({"memory_written": 2, "task_completed": 1}),
        elapsed=30.0,
    )

    assert "total=3" in summary
    assert "distinct_subjects=2" in summary
    assert "top_subjects=[('swarm.memory.written', 2), ('swarm.task.completed', 1)]" in summary
    assert "top_types=[('memory_written', 2), ('task_completed', 1)]" in summary
