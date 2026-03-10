# Swarm Control Plane Spec

Status: draft v0.1
Owner: Winnie
Date: 2026-03-08

## Goal

Establish a multi-gateway swarm control plane for fumemory/OpenClaw that can:
- register gateways and capabilities
- route tasks by lane and capability
- enforce single-owner execution with fencing
- require review/evaluation before completion
- attach artifacts and memory proof to every task
- support specialized optimization lanes such as Autoresearch

## Core objects

### Gateway Registry
Each execution node/gateway advertises:
- `gateway_id`
- `host`
- `environment`
- `status`
- `capabilities`
- `models`
- `tool_access`
- `max_concurrency`
- `current_load`
- `trust_tier`
- `last_heartbeat_at`
- `metadata`

### Task
Required fields:
- `task_id`
- `project`
- `title`
- `lane`
- `status`
- `priority`
- `requested_by`
- `acceptance_criteria`
- `evaluation_plan`
- `created_at`
- `updated_at`

Execution fields:
- `assigned_gateway`
- `assigned_agent`
- `fencing_token`
- `attempt_count`
- `depends_on`
- `input_refs`
- `artifact_refs`
- `result_summary`
- `score`
- `review_status`
- `reviewer`
- `memory_proof`

### Artifact
- `artifact_id`
- `task_id`
- `kind`
- `path`
- `sha256`
- `created_at`
- `created_by`
- `metadata`

### Review Record
- `review_id`
- `task_id`
- `reviewer`
- `decision`
- `findings`
- `created_at`

## Lifecycle

Task states:
- `queued`
- `claimed`
- `running`
- `blocked`
- `review`
- `done`
- `failed`
- `cancelled`

## Lane model

Initial lanes:
- `plan`
- `build`
- `test`
- `review`
- `research`
- `optimize`
- `deploy`

## Orchestration depth policy

Default hierarchy:
- `Level 1` — direct execution for simple work
- `Level 2` — one owner agent plus `2-5` worker agents for medium work
- `Level 3` — only for clearly partitioned large workstreams with explicit specialist subteams

Hard guidance:
- OpenClaw remains the top-level control plane
- one scoped task owner per lane/objective
- cap direct subagents at `3-5`
- deeper nesting is exceptional, not default
- every branch must have:
  - one clear scope
  - one owner
  - one output artifact
  - one verification step

Execution split:
- OpenClaw / lead agent -> coordination, routing, state, review chain
- OpenCode / Codex / Claude Code -> isolated implementation work inside bounded scopes

## Fencing / ownership

Claim writes must include:
- `assigned_gateway`
- `assigned_agent`
- `fencing_token`
- `claimed_at`

Later status writes must present the same fencing token or be rejected.

## NATS subjects

- `swarm.gateway.register`
- `swarm.gateway.heartbeat`
- `swarm.gateway.status`
- `swarm.tasks.create`
- `swarm.tasks.claim`
- `swarm.tasks.status`
- `swarm.tasks.review`
- `swarm.tasks.done`
- `swarm.tasks.fail`
- `swarm.artifacts.add`

Retained mesh status lives in JetStream KV bucket `GATEWAY_STATE`; readers should prefer that for current ownership / liveness and use subjects for live fan-out.

## Quality gates

A task cannot enter `done` unless:
- deliverable exists
- evaluation plan executed
- artifacts attached
- review exists for non-trivial tasks
- memory proof recorded

## Autoresearch integration

Autoresearch should be implemented as the `optimize` lane:
- one mutable target per run
- frozen evaluator descriptor
- keep/discard metric
- experiment ledger artifact
- promotion through review, not direct done

## Minimal rollout plan

### Phase 0
- freeze schemas
- create registry/task/artifact/review models

### Phase 1
- implement NATS claim/status protocol
- implement simple board/API

### Phase 2
- wire OpenClaw gateways to register/heartbeat
- add review workflow
- add optimize lane integration
