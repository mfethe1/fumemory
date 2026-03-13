# Strict Sub-Agent Context Isolation

## Overview

This document describes the **Strict Sub-Agent Context Isolation** system implemented in fumemory to enforce shallow orchestration and prevent context bleed in NATS payload handoffs.

## Problem Statement

### Context Bleed
When sub-agents communicate via NATS, there's a risk of **context bleed** — passing unnecessary historical context, full DAG state, or upstream reasoning that:
- Slows down message processing
- Increases memory usage
- Creates coupling between agents
- Violates the principle of shallow orchestration

### IPv6/IPv4 Connection Issues
NATS connections can fail when the system attempts to use IPv6 but the network only supports IPv4, or vice versa. This causes intermittent connection failures.

## Solution

### 1. Context Isolation Module (`memu/context_isolation.py`)

Enforces strict payload boundaries through:

#### Payload Sanitization
- **Strips context snapshots**: Removes `context_snapshot`, `memory_version_hashes`, `blackboard_entries`
- **Truncates long strings**: Limits strings to 512 chars max
- **Limits metadata**: Max 10 metadata keys per payload
- **Replaces checkpoint state**: Full checkpoint state replaced with 64-char pointer

#### Size Limits
- **Payload limit**: 8KB max per payload
- **Event limit**: 16KB max per complete event
- **String limit**: 512 chars max per string field

#### API Functions

```python
from memu.context_isolation import (
    strip_context_bleed,
    enforce_payload_size_limit,
    sanitize_agent_message,
    create_minimal_task_handoff,
)

# Strip context bleed from payload
sanitized = strip_context_bleed(payload)

# Enforce size limit
enforce_payload_size_limit(payload)  # Raises ContextIsolationError if too large

# Sanitize agent-to-agent messages
clean_msg = sanitize_agent_message(message)

# Create minimal task handoff (canonical way)
handoff = create_minimal_task_handoff(
    task_id=task_id,
    agent_id="lenny",
    title="Do this task",
    description="Details here",
)
```

### 2. IPv4-First Connection Strategy

All NATS connections now use IPv4-first resolution:

#### Implementation
1. Parse NATS URL to extract hostname and port
2. Resolve hostname to IPv4 address using `socket.getaddrinfo(AF_INET)`
3. Reconstruct URL with IPv4 address
4. Fall back to original URL if resolution fails

#### Files Updated
- `memu/cluster.py` - NATSClusterManager
- `memu/nats_worker.py` - Worker connection
- `memu/event_consumer.py` - Consumer connection

### 3. Publisher Integration

`memu/nats_publisher.py` now automatically:
- Sanitizes all payloads before publishing
- Enforces size limits
- Logs violations
- Rejects events > 16KB

## Usage Examples

### Publishing Events with Context Isolation

```python
from memu.nats_publisher import NATSEventPublisher
from memu.cluster import NATSClusterManager

cluster = NATSClusterManager(
    local_url="nats://localhost:4222",
    railway_url="nats://railway.example.com:4222",
)
await cluster.connect()

publisher = NATSEventPublisher(cluster, gateway_id="winnie")

# Payload is automatically sanitized
await publisher.publish_task_claimed(
    agent_id="winnie",
    task_id="task-123",
    title="Fix the bug",
)
```

### Creating Minimal Task Handoffs

```python
from memu.context_isolation import create_minimal_task_handoff

# This is the canonical way to hand off tasks between sub-agents
handoff = create_minimal_task_handoff(
    task_id=task_id,
    agent_id="lenny",
    title="Build the feature",
    description="Implement user authentication",
    priority="high",
    deadline="2026-03-15",
)

# Handoff is guaranteed to be < 8KB and context-free
await nc.publish("swarm.agent.lenny.inbox", json.dumps(handoff).encode())
```

## Testing

Run the context isolation tests:

```bash
pytest tests/test_context_isolation.py -v
```

## Benefits

### Speed
- Smaller payloads = faster network transfer
- Less parsing overhead
- Reduced memory usage

### Focus
- Sub-agents receive only what they need
- No distraction from historical context
- Clear task boundaries

### Reliability
- IPv4-first strategy prevents connection failures
- Automatic fallback to original URL
- Better error messages

## Configuration

Environment variables (optional):

```bash
# Override default limits (not recommended)
export CONTEXT_ISOLATION_MAX_PAYLOAD_BYTES=8192
export CONTEXT_ISOLATION_MAX_STRING_LENGTH=512
export CONTEXT_ISOLATION_MAX_METADATA_KEYS=10
```

## Monitoring

Check logs for context isolation violations:

```bash
# Look for oversized payloads
grep "Context isolation violation" logs/memu.log

# Look for IPv4 resolution
grep "Resolved.*to IPv4" logs/memu.log

# Look for connection failures
grep "NATS connect failed" logs/memu.log
```

## Best Practices

1. **Always use `create_minimal_task_handoff()`** for task delegation
2. **Never include `context_snapshot` in payloads** — use `context_pointer` instead
3. **Keep descriptions under 512 chars** — link to full docs if needed
4. **Use metadata sparingly** — max 10 keys
5. **Test payload size** before publishing custom events

## Migration Guide

If you have existing code that publishes to NATS:

### Before
```python
payload = {
    "task_id": task_id,
    "agent_id": agent_id,
    "title": title,
    "context_snapshot": full_context,  # ❌ Context bleed
    "checkpoint_state": large_state,    # ❌ Too large
    "metadata": {f"key_{i}": i for i in range(100)},  # ❌ Too many keys
}
```

### After
```python
from memu.context_isolation import create_minimal_task_handoff

payload = create_minimal_task_handoff(
    task_id=task_id,
    agent_id=agent_id,
    title=title,
    description=description[:512],
    context_pointer=context_id,  # ✅ Pointer only
    checkpoint_pointer=checkpoint_hash,  # ✅ Hash only
)
```

## See Also

- [NATS Connection Guide](./MULTI_MACHINE.md)
- [Swarm Control Plane Spec](./SWARM_CONTROL_PLANE_SPEC.md)
- [Agent Mesh Skill](../skills/agent-mesh/SKILL.md)

