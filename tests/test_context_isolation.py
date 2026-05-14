"""Tests for strict sub-agent context isolation."""

import pytest
from uuid import uuid4

from memu.context_isolation import (
    strip_context_bleed,
    enforce_payload_size_limit,
    sanitize_agent_message,
    create_minimal_task_handoff,
    ContextIsolationError,
    MAX_STRING_LENGTH,
)


def test_strip_context_snapshot():
    """Context snapshots should be completely removed."""
    payload = {
        "task_id": "123",
        "agent_id": "winnie",
        "context_snapshot": {
            "memory_ids": ["a", "b", "c"],
            "memory_version_hashes": ["hash1", "hash2"],
            "blackboard_entries": ["entry1"],
        },
        "title": "Test task",
    }
    
    sanitized = strip_context_bleed(payload)
    
    assert "context_snapshot" not in sanitized
    assert "task_id" in sanitized
    assert "agent_id" in sanitized
    assert "title" in sanitized


def test_strip_blackboard_entries():
    """Blackboard entries should be removed to prevent shared state leakage."""
    payload = {
        "task_id": "123",
        "blackboard_entries": ["entry1", "entry2"],
        "memory_version_hashes": ["hash1", "hash2"],
    }
    
    sanitized = strip_context_bleed(payload)
    
    assert "blackboard_entries" not in sanitized
    assert "memory_version_hashes" not in sanitized


def test_truncate_long_strings():
    """Long strings should be truncated to prevent payload bloat."""
    long_string = "x" * 1000
    payload = {
        "description": long_string,
        "title": "Short",
    }
    
    sanitized = strip_context_bleed(payload)
    
    assert len(sanitized["description"]) <= MAX_STRING_LENGTH + 20  # +20 for "[truncated]"
    assert sanitized["title"] == "Short"


def test_strip_checkpoint_state():
    """Full checkpoint state should be replaced with pointer."""
    large_checkpoint = "x" * 10000
    payload = {
        "checkpoint_state": large_checkpoint,
        "task_id": "123",
    }
    
    sanitized = strip_context_bleed(payload)
    
    assert "checkpoint_state" not in sanitized
    assert "checkpoint_pointer" in sanitized
    assert len(sanitized["checkpoint_pointer"]) == 64  # Hash/ID only


def test_limit_metadata_keys():
    """Metadata should be limited to prevent bloat."""
    large_metadata = {f"key_{i}": f"value_{i}" for i in range(50)}
    payload = {
        "metadata": large_metadata,
        "task_id": "123",
    }
    
    sanitized = strip_context_bleed(payload)
    
    assert len(sanitized["metadata"]) <= 10


def test_enforce_payload_size_limit_pass():
    """Small payloads should pass size check."""
    payload = {
        "task_id": "123",
        "agent_id": "winnie",
        "title": "Test",
    }
    
    # Should not raise
    enforce_payload_size_limit(payload)


def test_enforce_payload_size_limit_fail():
    """Oversized payloads should raise ContextIsolationError."""
    # Create a payload that exceeds 8KB
    large_payload = {
        "data": "x" * 10000,
    }
    
    with pytest.raises(ContextIsolationError):
        enforce_payload_size_limit(large_payload)


def test_sanitize_agent_message():
    """Agent messages should be sanitized to allowed keys only."""
    message = {
        "msg_id": "123",
        "from": "winnie",
        "to": "lenny",
        "type": "request",
        "task_id": "456",
        "context_snapshot": {"memory_ids": ["a", "b"]},  # Should be removed
        "blackboard_entries": ["entry1"],  # Should be removed
        "payload": {
            "message": "Do this task",
            "context_snapshot": {"foo": "bar"},  # Should be removed
        },
    }
    
    sanitized = sanitize_agent_message(message)
    
    assert "msg_id" in sanitized
    assert "from" in sanitized
    assert "to" in sanitized
    assert "type" in sanitized
    assert "task_id" in sanitized
    assert "context_snapshot" not in sanitized
    assert "blackboard_entries" not in sanitized
    assert "payload" in sanitized
    assert "context_snapshot" not in sanitized["payload"]


def test_create_minimal_task_handoff():
    """Minimal task handoffs should be clean and small."""
    task_id = uuid4()
    
    handoff = create_minimal_task_handoff(
        task_id=task_id,
        agent_id="lenny",
        title="Test task",
        description="Do something",
        priority="high",
    )
    
    assert handoff["task_id"] == str(task_id)
    assert handoff["agent_id"] == "lenny"
    assert handoff["title"] == "Test task"
    assert handoff["description"] == "Do something"
    assert handoff["priority"] == "high"
    
    # Should pass size check
    enforce_payload_size_limit(handoff)


def test_create_minimal_task_handoff_truncates():
    """Long titles and descriptions should be truncated."""
    long_title = "x" * 500
    long_description = "y" * 1000
    
    handoff = create_minimal_task_handoff(
        task_id=uuid4(),
        agent_id="lenny",
        title=long_title,
        description=long_description,
    )
    
    assert len(handoff["title"]) == 256
    assert len(handoff["description"]) == 512

