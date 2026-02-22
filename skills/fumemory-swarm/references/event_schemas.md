# SwarmEvent Schemas Reference

## Event Envelope (all events)

Every NATS message on `swarm.events.*` MUST conform to this envelope:

```json
{
  "event_id": "string (UUID v4)",
  "timestamp": "string (ISO-8601 with timezone)",
  "source_gateway": "string (agent name: winnie|lenny|mack|rosie|coordinator|glass_box_ui)",
  "event_type": "string (see Event Types below)",
  "task_id": "string (UUID v4)",
  "payload": "object (event-specific, see below)",
  "compute_cost": "float (USD cost of this operation, 0.0 if free)"
}
```

## Event Types & Payloads

### task_drafted
```json
{
  "title": "string",
  "root_prompt_id": "UUID",
  "parent_task_id": "UUID | null",
  "compute_budget": "float (max USD for this task)"
}
```

### bid_submitted
```json
{ "gateway_id": "string" }
```

### lease_granted
```json
{ "gateway_id": "string" }
```

### task_claimed
```json
{ "gateway_id": "string" }
```

### task_completed
```json
{ "result": "string (completion summary)" }
```

### task_failed
```json
{ "error": "string", "retry_count": "int" }
```

### task_cancelled
```json
{ "reason": "string" }
```

### task_amended
```json
{ "title": "string (optional)", "correction": "string" }
```

### audit_proposed
```json
{ "auditor_gateway": "string", "findings": "string" }
```

### audit_accepted / audit_rejected
```json
{ "auditor_gateway": "string", "reason": "string" }
```

### lease_expired
```json
{ "gateway_id": "string", "expired_at": "ISO-8601" }
```

### circuit_breaker
```json
{ "gateway_id": "string", "failure_count": "int", "threshold": "int" }
```

### system_halt
```json
{
  "root_prompt_id": "UUID",
  "halt_reason": "string",
  "override_instruction": "string | null",
  "initiated_by": "string (user|system|medic)"
}
```

### system_override
```json
{ "correction": "string", "initiated_by": "string" }
```

### heartbeat_ping
Published to `swarm.heartbeat`, not `swarm.events.*`:
```json
{
  "gateway_id": "string",
  "uptime_s": "float",
  "active_tasks": "int",
  "memory_mb": "float"
}
```

## NATS KV Buckets

### RESOURCE_LANES
- Key: lane name (e.g., `frontend`, `qa`, `research`, `infra`)
- Value: `{"holder": "gateway_id", "fencing_token": int, "acquired_at": "ISO-8601"}`
- TTL: 10 seconds (Dead Man's Switch — must refresh every 3s)

### FENCING_TOKENS
- Key: lane name
- Value: monotonically increasing integer
- Purpose: prevent split-brain stale writes (any write with token < current is rejected)
