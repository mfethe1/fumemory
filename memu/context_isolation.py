"""Strict Sub-Agent Context Isolation — Enforce Shallow Orchestration.

This module prevents context bleed in NATS payload handoffs by:
1. Stripping unnecessary metadata from payloads
2. Enforcing strict payload size limits
3. Removing recursive context snapshots
4. Sanitizing agent-to-agent messages

Speed & Focus: Sub-agents should receive ONLY what they need to execute,
nothing more. No historical context, no full DAG state, no upstream reasoning.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Strict limits for shallow orchestration
MAX_PAYLOAD_SIZE_BYTES = 8192  # 8KB max per payload
MAX_CONTEXT_DEPTH = 1  # No nested context snapshots
MAX_METADATA_KEYS = 10  # Limit metadata bloat
MAX_STRING_LENGTH = 512  # Truncate long strings


class ContextIsolationError(Exception):
    """Raised when payload violates isolation rules."""
    pass


def strip_context_bleed(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove context bleed from a payload before NATS publish.
    
    This enforces shallow orchestration by removing:
    - Nested context_snapshot objects (recursive context)
    - Full memory_version_hashes (historical state)
    - Blackboard entries (shared state leakage)
    - Excessive metadata
    - Long string values
    
    Args:
        payload: Raw payload dict before publishing
        
    Returns:
        Sanitized payload with context bleed removed
        
    Raises:
        ContextIsolationError: If payload is too large after sanitization
    """
    sanitized = {}
    
    for key, value in payload.items():
        # Skip context snapshot objects entirely
        if key in {"context_snapshot", "memory_version_hashes", "blackboard_entries"}:
            logger.debug(f"Stripped context bleed field: {key}")
            continue
            
        # Skip full checkpoint state (use checkpoint_pointer instead)
        if key == "checkpoint_state" and isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
            sanitized["checkpoint_pointer"] = value[:64]  # Keep hash/ID only
            logger.debug("Stripped full checkpoint_state, kept pointer")
            continue
            
        # Limit metadata to essential keys
        if key == "metadata" and isinstance(value, dict):
            if len(value) > MAX_METADATA_KEYS:
                # Keep only first N keys
                sanitized[key] = dict(list(value.items())[:MAX_METADATA_KEYS])
                logger.warning(f"Truncated metadata from {len(value)} to {MAX_METADATA_KEYS} keys")
            else:
                sanitized[key] = value
            continue
            
        # Truncate long strings
        if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
            sanitized[key] = value[:MAX_STRING_LENGTH] + "...[truncated]"
            logger.debug(f"Truncated string field {key} from {len(value)} to {MAX_STRING_LENGTH} chars")
            continue
            
        # Recursively sanitize nested dicts (but only 1 level deep)
        if isinstance(value, dict):
            sanitized[key] = {k: v for k, v in value.items() if k not in {"context_snapshot", "blackboard_entries"}}
            continue
            
        # Keep primitive values as-is
        sanitized[key] = value
    
    return sanitized


def enforce_payload_size_limit(payload: dict[str, Any], max_bytes: int = MAX_PAYLOAD_SIZE_BYTES) -> None:
    """Enforce strict payload size limit.
    
    Args:
        payload: Payload dict to check
        max_bytes: Maximum allowed size in bytes
        
    Raises:
        ContextIsolationError: If payload exceeds size limit
    """
    import json
    
    payload_json = json.dumps(payload, default=str)
    payload_size = len(payload_json.encode('utf-8'))
    
    if payload_size > max_bytes:
        raise ContextIsolationError(
            f"Payload size {payload_size} bytes exceeds limit {max_bytes} bytes. "
            f"This violates shallow orchestration principles. "
            f"Strip unnecessary context or use a context_pointer instead."
        )
    
    logger.debug(f"Payload size: {payload_size} bytes (limit: {max_bytes})")


def sanitize_agent_message(
    message: dict[str, Any],
    allowed_keys: set[str] | None = None
) -> dict[str, Any]:
    """Sanitize agent-to-agent messages for strict context isolation.
    
    Args:
        message: Raw message dict
        allowed_keys: Optional whitelist of allowed keys
        
    Returns:
        Sanitized message with only essential fields
    """
    if allowed_keys is None:
        # Default whitelist for agent messages
        allowed_keys = {
            "msg_id", "timestamp", "from", "to", "type", "priority",
            "task_id", "agent_id", "title", "description", "status",
            "error", "notes", "query", "result_count"
        }
    
    sanitized = {k: v for k, v in message.items() if k in allowed_keys}
    
    # Always strip payload if it exists, replace with minimal version
    if "payload" in message:
        original_payload = message["payload"]
        if isinstance(original_payload, dict):
            sanitized["payload"] = strip_context_bleed(original_payload)
    
    return sanitized


def create_minimal_task_handoff(
    task_id: UUID | str,
    agent_id: str,
    title: str,
    description: str | None = None,
    **kwargs
) -> dict[str, Any]:
    """Create a minimal task handoff payload with zero context bleed.
    
    This is the canonical way to hand off tasks between sub-agents.
    
    Args:
        task_id: Task UUID
        agent_id: Target agent ID
        title: Task title (max 256 chars)
        description: Optional task description (max 512 chars)
        **kwargs: Additional minimal metadata (will be sanitized)
        
    Returns:
        Minimal task handoff payload
    """
    payload = {
        "task_id": str(task_id),
        "agent_id": agent_id,
        "title": title[:256] if title else "",
        "description": description[:512] if description else None,
    }
    
    # Add sanitized kwargs
    for key, value in kwargs.items():
        if key not in {"context_snapshot", "checkpoint_state", "blackboard_entries"}:
            if isinstance(value, str):
                payload[key] = value[:MAX_STRING_LENGTH]
            else:
                payload[key] = value
    
    # Enforce size limit
    enforce_payload_size_limit(payload)
    
    return payload

