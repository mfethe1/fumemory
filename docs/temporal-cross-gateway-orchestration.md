# Temporal Cross-Gateway Orchestration

Status: implemented MVP for durable async routes + multi-gateway handoff visibility.
Owner lane: Macklemore infra / Winnie coordination assist.

## What this adds

This repo already had basic Temporal-backed async memory/search routes, but the contracts were too loose for multi-gateway operation:
- workflow IDs were based on Python `hash()` (process-randomized)
- `/search/async` dropped most of the `SearchRequest` contract on the floor
- API import paths hard-failed when `temporalio` was not installed locally
- worker/bootstrap config was hard-coded to a single queue/namespace
- Temporal progress was not mirrored into memU task events/checkpoints for failover visibility

This pass tightens those edges.

## Canonical request contract

Shared models now live in `memu/temporal_contracts.py`.

### `OrchestrationContext`
Optional cross-gateway context that can be attached to `MemoryCreate` and `SearchRequest`:
- `task_id`
- `source_gateway`
- `lease_key`
- `parent_event_id`
- `checkpoint_scope`
- `metadata`

Back-compat note: for memory writes, the client also reads these keys from `metadata` if older callers have not yet moved to the explicit `orchestration` field.

## Async route behavior

### `POST /memories/async`
Returns a stable accepted payload:
- `status`
- `workflow_id`
- `task_queue`
- `namespace`
- `route_version`

### `POST /search/async`
Now forwards the full `SearchRequest` contract into Temporal instead of only `{query, agent_id}`.
Current worker support preserves:
- `query`
- `limit`
- `agent_id`
- `memory_type`
- `min_confidence`
- `temporal_weight`
- `orchestration`

## Worker/bootstrap wiring

Environment-driven settings:
- `TEMPORAL_HOST`
- `TEMPORAL_NAMESPACE`
- `TEMPORAL_TASK_QUEUE`
- `TEMPORAL_WORKER_IDENTITY`
- `GATEWAY_ID`
- `GATEWAY_ROLE`

`docker-compose.yml` now wires the API and worker through the same host/namespace/queue settings so multiple gateways can attach workers to the same queue intentionally.

## Checkpoint + event contracts

When `orchestration.task_id` is present, Temporal workflows emit durable progress into memU:

### Checkpoints (`checkpoints` table + `checkpoint_saved` event)
At minimum:
- accepted
- embedding complete
- search/store complete
- final completion or failure

Stored payload shape:
- `task_id`
- `workflow_id`
- `gateway_id`
- `checkpoint_sequence`
- `step`
- `status`
- `scratchpad`
- `progress_pct`
- `tokens_consumed`
- `metadata`

### Task events (`events` table)
Emitted event types:
- `task_claimed`
- `task_completed`
- `task_failed`
- implicit `checkpoint_saved` via checkpoint activity

These records give Warden / forensics / bridge consumers a canonical durable trail even when a Temporal worker continues on a different node.

## Retry / failover behavior

Activities now use explicit Temporal retry policies:
- general activities: 3 attempts, exponential backoff
- checkpoint/event persistence: 2 attempts, shorter backoff

Failure path:
1. workflow catches the exception
2. writes a terminal failed checkpoint when task context exists
3. records `task_failed`
4. re-raises so Temporal keeps the failure visible to operators

This means cross-gateway continuation remains durable, but we still preserve forensic evidence instead of silently swallowing failures.

## Known remaining gaps

1. `/search/async` is closer to `/search`, but not yet full parity with the sync expansion schedule + lexical tie-break fallback path.
2. Workflow progress is mirrored into Postgres events/checkpoints, but not yet fanned out to NATS live subjects from the Temporal worker.
3. There is still no dedicated Temporal workflow for gateway lease renewal / lease takeover orchestration; current support focuses on durable task context + failover visibility for async memory/search flows.
4. The worker logs an identity string for observability, but we do not yet thread that identity into Temporal worker versioning/build-id rollout controls.
