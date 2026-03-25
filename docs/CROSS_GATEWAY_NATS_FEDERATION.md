# Cross-Gateway NATS Federation Spec

Status: **shareable reference**  
Owner: Macklemore  
Last updated: 2026-03-25

## Purpose

This document is the **single rollout spec** for letting multiple OpenClaw gateways connect to the same Railway-backed NATS/JetStream system and dispatch swarms together.

It is intentionally boring and explicit.

If a gateway matches this document, it is allowed onto the shared swarm bus.
If it does not match this document, it is **not** considered swarm-ready.

---

## Scope

This spec covers:
- how each gateway connects to shared Railway NATS
- which env/config values are required
- which subjects are allowed for shared swarm traffic
- what a dispatch envelope must contain
- how gateways announce themselves
- how to verify a gateway before joining the mesh

This spec does **not** replace:
- memU API contract docs
- local-only NATS dev setup docs
- OPA/Rego authorization policy implementation details

---

## Architecture

```text
┌─────────────────────┐
│ Gateway A           │
│ OpenClaw host       │
│ memU hooks          │
│ swarm publisher     │
└─────────┬───────────┘
          │
          │ Railway NATS / JetStream
          │ (shared control plane)
          ▼
┌─────────────────────────────────────┐
│ Shared streams / subjects           │
│ - AGENT_EVENTS                      │
│ - swarm.discovery                   │
│ - swarm.events                      │
│ - swarm.rpc.request                 │
│ - swarm.rpc.response.<gateway_id>   │
│ - swarm.warden.*                    │
│ - swarm.advisory.suicide.*          │
└─────────┬───────────────────────────┘
          │
          │
┌─────────▼───────────┐    ┌──────────▼──────────┐
│ Gateway B           │    │ Gateway C           │
│ OpenClaw host       │    │ OpenClaw host       │
│ warm standby / work │    │ worker / specialist │
└─────────────────────┘    └─────────────────────┘
```

Design rule:
- **Railway NATS is the shared bus** for cross-gateway coordination.
- Local NATS may still exist for dev/latency, but it is **not** the federation reference.
- A gateway joins the federation by connecting to the **shared Railway NATS URL**, not by discovering another gateway's local broker.

---

## Required gateway prerequisites

A gateway is eligible for federation only if all are true:

1. It uses the **golden memU contract**
   - Railway memU base URL
   - canonical write path `/api/v1/memu/add`
   - `X-MemU-Key` primary auth
   - `X-API-Key` compatibility auth when needed

2. It has no localhost-only production defaults for memU or NATS fallback behavior that silently bypass the shared system.

3. It has a stable `gateway_id` that does not change across restarts.

4. It can publish and consume against Railway NATS.

5. It passes the smoke tests in this doc.

---

## Environment contract

Each gateway joining the shared swarm bus must provide these values.

### Required

- `GATEWAY_ID`
  - unique, stable, machine-readable
  - examples: `mac-mini-main`, `winnie-windows`, `rosie-mini`

- `NATS_RAILWAY_URL`
  - full Railway NATS connection URL
  - required for shared federation
  - must fail loud if absent

### Recommended / supported

- `NATS_AUTH_TOKEN`
  - token auth for Railway NATS if enabled

- `NATS_LOCAL_URL`
  - optional local fast-path broker for local/dev failover
  - must **not** be treated as the federation reference

- `AGENT_EVENTS_STREAM`
  - default: `AGENT_EVENTS`

- `AGENT_EVENTS_CONSUMER`
  - unique durable per gateway or service
  - recommended format: `<gateway_id>_AGENT_EVENTS`

- `AGENT_EVENTS_SUBJECT`
  - default: `AGENT_EVENTS`

### Naming rules

- `GATEWAY_ID` must match: `[a-z0-9-]+`
- no spaces
- no user-controlled input
- no random per-boot suffixes

---

## Canonical subjects

These are the shared subjects other gateways are allowed to depend on.

### Discovery / presence
- `swarm.discovery`
- `swarm.warden.heartbeat`
- `swarm.events.heartbeat`

### Core swarm bus
- `swarm.events`
- `AGENT_EVENTS`

### Directed RPC
- `swarm.rpc.request`
- `swarm.rpc.response.<gateway_id>`

### Coordination / control
- `swarm.warden.respawn`
- `swarm.warden.status`
- `swarm.advisory.suicide.<gateway_id>`

### Optional / advanced
- `swarm.blackboard`
- `swarm.blackboard.sync`
- `swarm.blackboard.validate`
- `swarm.checkpoint.<task_id>`

### Subject policy

Allowed:
- fixed subjects above
- parameterized subjects where the parameter is system-generated (`gateway_id`, `task_id`)

Not allowed:
- subjects built from raw user input
- wildcard publish subjects
- ad hoc per-prompt subject invention

If a gateway needs a new subject, add it to this file first.

---

## Dispatch contract

All cross-gateway swarm dispatches must use a structured envelope.

## Minimum envelope

```json
{
  "version": 1,
  "dispatch_id": "uuid",
  "root_task_id": "task_123",
  "source_gateway_id": "mac-mini-main",
  "target_gateway_id": "rosie-mini",
  "kind": "task.dispatch",
  "ts": "2026-03-25T19:00:00Z",
  "ttl_seconds": 300,
  "idempotency_key": "root_task_id:target_gateway_id:step_name",
  "capabilities": ["research", "browser", "qa"],
  "payload": {
    "title": "Run cross-gateway research sweep",
    "prompt": "...",
    "priority": "normal",
    "budget": {
      "max_agents": 3,
      "max_runtime_seconds": 600
    }
  }
}
```

## Required fields

- `version`
- `dispatch_id`
- `root_task_id`
- `source_gateway_id`
- `kind`
- `ts`
- `ttl_seconds`
- `idempotency_key`
- `payload`

## Required behavior

- receivers must reject expired messages
- receivers must reject duplicate `idempotency_key` replays
- receivers must reject messages targeting unknown gateways
- receivers must fail loud on schema mismatch

## Recommended kinds

- `task.dispatch`
- `task.accepted`
- `task.started`
- `task.checkpoint`
- `task.completed`
- `task.failed`
- `gateway.announce`
- `gateway.status`

---

## Gateway announce contract

When a gateway boots or its capabilities change, it should publish an announcement on `swarm.discovery`.

### Minimum announce payload

```json
{
  "gateway_id": "mac-mini-main",
  "ts": "2026-03-25T19:00:00Z",
  "role": "general",
  "capabilities": ["memu", "swarm", "browser", "exec"],
  "status": "ready",
  "version": "1",
  "transport": {
    "railway_nats": true,
    "agent_events": true
  }
}
```

### Readiness rule

A gateway is considered swarm-dispatchable only after:
1. successful Railway NATS connection
2. successful announce on `swarm.discovery`
3. successful heartbeat publication
4. successful consume/probe verification

---

## Durable consumer rules

Each gateway or service consuming shared streams must use a **unique durable name**.

### Required

- durable consumer names must not collide across gateways
- one gateway must not reuse another gateway's durable consumer name
- consumer names should encode ownership

### Recommended format

- `MEMU_<GATEWAY_ID>_AGENT_EVENTS`
- `MEMU_<GATEWAY_ID>_SWARM_EVENTS`

### Example

- `MEMU_MAC_MINI_MAIN_AGENT_EVENTS`
- `MEMU_ROSIE_MINI_AGENT_EVENTS`

Reason:
- avoids invisible message theft / ack confusion across machines
- makes recovery and replay sane

---

## Security / authorization expectations

This spec assumes subject-level auth will be enforced with OPA/Rego or equivalent policy.

Minimum intent:
- a gateway may publish discovery/status for itself
- a gateway may publish swarm events under approved subjects
- a gateway may consume shared events it is authorized to process
- directed response subjects must be scoped to the intended `gateway_id`

Until full policy enforcement is wired, treat the federation as **trusted internal infrastructure only**.

Do not expose Railway NATS publicly without auth.

---

## Smoke tests for onboarding a gateway

Run these in order.

### 1. Config proof
- `GATEWAY_ID` present
- `NATS_RAILWAY_URL` present
- no silent localhost prod default masking shared Railway path

### 2. Connection proof
- connect to Railway NATS successfully
- JetStream context available

### 3. Publish proof
- publish a discovery payload to `swarm.discovery`
- publish a test event to `swarm.events`

### 4. Consume proof
- read back from expected stream / subject via dedicated durable consumer
- verify the message is visible to the intended consumer path

### 5. Directed response proof
- publish request → receive response on `swarm.rpc.response.<gateway_id>`

### 6. Idempotency proof
- replay the same `idempotency_key`
- verify duplicate is ignored or rejected deterministically

### 7. memU side proof
- write related marker to memU
- verify it is searchable after dispatch

A gateway is not “ready” until all 7 pass.

---

## Operational rollout order

Roll gateways one at a time.

1. Mac golden reference
2. next stable gateway
3. next stable gateway
4. Windows / harder-to-reach hosts last

For each gateway:
1. patch config
2. restart cleanly
3. wait for settle
4. run fresh smoke tests
5. record proof artifact
6. only then mark swarm-ready

Do not bulk-roll all gateways at once.

---

## Failure handling

If Railway NATS is unavailable:
- fail loud
- mark gateway `degraded`
- do not claim swarm-ready state
- do not silently route cross-gateway dispatches to localhost-only brokers

If a gateway cannot prove discovery/publish/consume:
- do not register it as available for shared dispatch

If idempotency is broken:
- stop rollout
- fix before enabling shared swarms

---

## Done criteria

The cross-gateway NATS method is considered ready when:

- at least 2 gateways pass this full onboarding spec
- both can publish/consume against Railway NATS
- both can exchange directed RPC on `swarm.rpc.*`
- both can dispatch a test swarm task with idempotent completion semantics
- both emit searchable memU evidence tied to the dispatch

Until then, the method is still implementation-stage, not fleet-ready.

---

## Implementation artifacts in this repo

- Smoke verifier: `scripts/gateway_federation_smoke.py`
- Federation helpers/contract utilities: `memu/gateway_federation.py`
- OPA/Rego subject policy: `memu/policy/rules/jetstream_authz.rego`

Example:

```bash
GATEWAY_ID=mac-mini-main \
NATS_RAILWAY_URL='nats://...' \
NATS_AUTH_TOKEN='...' \
MEMU_BASE_URL='https://api-production-86f5.up.railway.app' \
X_MEMU_KEY='...' \
python scripts/gateway_federation_smoke.py --json
```

## Recommended next implementation steps

1. wire subject-level OPA/Rego enforcement to this exact subject list
2. run `scripts/gateway_federation_smoke.py` during onboarding for every gateway
3. standardize durable consumer naming in code
4. add replay/idempotency assertions to CI
5. add a lightweight federation status dashboard / artifact output

---

## Non-goals

This spec is not trying to:
- expose arbitrary remote execution across the internet
- let gateways invent their own transport rules
- let LLM output define subjects or auth behavior
- replace OpenClaw session routing

It is only the **shared NATS federation contract** for multi-gateway swarm coordination.
