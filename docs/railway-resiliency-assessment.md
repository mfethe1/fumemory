# Railway Resiliency / Redundancy Assessment

Date: 2026-03-10
Owner: Lenny

## What was validated

### 1) Gateway status plane now has a retained source of truth
Validated from `memu/state_manager.py`, `memu/ws_bridge.py`, `memu/api.py`, and tests:
- gateways publish retained state into JetStream KV bucket `GATEWAY_STATE`
- real-time fan-out uses `swarm.gateway.register`, `swarm.gateway.heartbeat`, and `swarm.gateway.status`
- warm-standby gateways remain visible without needing a task-bound heartbeat loop
- both automation and dashboard views exist:
  - `GET /api/v1/gateways/status`
  - `GET /api/gateways/status`
- stale/offline mapping is explicit instead of inferred by cron:
  - `>=20s` => degraded
  - `>=60s` => offline

Passing validation:
- `PYTHONPATH=. pytest -q tests/test_gateway_status_sync.py tests/test_bridge_ledger_gateway_status.py tests/test_nats_cluster_urls.py`

### 2) Failover visibility is materially better
The status overview includes:
- `active_nats_node`
- per-node connection info from `NATSClusterManager.status()`
- alert codes for:
  - `cluster_on_fallback`
  - `cluster_nodes_disconnected`
  - `gateway_stale`
  - `gateway_offline`

This is enough for Macklemore / automation to see when the mesh is alive but running on fallback.

### 3) Whole-system local testability had two concrete topology issues
Fixed in this branch:
- `NATSClusterManager` no longer strips `nats://nats:4222` and Railway private DNS hosts as “placeholders”
  - this was blocking Compose/Railway service-DNS usage for every component that relies on the cluster manager
- `bridge` in `docker-compose.yml` now receives NATS env vars and waits for NATS health
  - previously the status bridge could boot disconnected even when the rest of the stack was healthy

## Deployability assessment

## Status: **partially deployable**

The gateway status plane itself is now deployable and testable.
The full cross-gateway resiliency story is **not** fully deployable on Railway yet.

## Recommended Railway topology

### Railway-hosted control plane
Run these on Railway:
- `api`
- `postgres` / pgvector
- `nats` with JetStream + persistent volume
- `ws_bridge`
- `projector`
- `event-consumer`
- optional `temporal` + `temporal-worker`

### Gateway/worker placement
Use a mixed topology:
- at least one Railway-resident gateway/worker for continuity when local infra is down
- local/mac gateways remain primary when low-latency/local tools are needed
- all gateways publish retained status into the same Railway-visible control plane

### Redundancy expectation
Treat Railway as:
- a **control-plane survivability layer**
- a **fallback NATS / visibility plane**
- not true multi-node NATS HA by itself

## Remaining gaps

### Gap 1 — JetStream event stream provisioning is still assumed, not guaranteed
`memu/projector.py` uses a durable pull consumer on `swarm.events`, but this repo does not currently guarantee creation of the backing JetStream stream.

Impact:
- KV-backed retained gateway state works
- durable event projection can still fail or start empty if the stream is absent

Needed:
- explicit bootstrap for a `swarm.events` stream during startup or deploy
- documented subject set and retention policy
- a smoke test that proves projector replay after restart

### Gap 2 — Warden failover is Docker-native, not Railway-native
`memu/warden_runtime.py` can detect dead gateways, publish orphaning, and request respawn, but actual replacement spawn still depends on Docker container launch semantics.

Impact on Railway:
- Railway services do not expose the same local Docker control path as the dev host
- Warden can observe failure but may be unable to create a replacement container there

Needed:
- Railway-aware respawn strategy:
  - either pre-provision always-on standby gateways
  - or integrate Railway service restart/deployment APIs
  - or move gateway respawn responsibility out of Warden for Railway deployments

### Gap 3 — Single Railway NATS instance is failover, not full HA
Current design improves survivability versus local-only NATS, but a single Railway NATS service with one volume is still a single failure domain.

Needed if true redundancy is required:
- external/managed multi-node NATS or equivalent
- or acceptance that Railway provides regional fallback visibility, not quorum-grade HA

### Gap 4 — Projector / consumer topology is not yet failover-aware enough
`projector` uses a single `NATS_URL`, not the dual-endpoint cluster manager.

Impact:
- API and gateways may fail over across NATS nodes
- projector can still be pinned to one node and miss the active control plane

Needed:
- migrate projector to `NATSClusterManager`
- verify behavior when local dies and Railway becomes active
- add replay/idempotency checks for reconnects

### Gap 5 — No end-to-end failover test harness yet
We now have good unit coverage for the retained status plane, but not a whole-system test that proves:
1. local gateway registers
2. heartbeats stop
3. overview marks degraded/offline
4. active NATS node changes to fallback
5. dashboard/API surface the alert
6. replacement worker remains visible

Needed:
- docker-compose or pytest-driven failover scenario test
- optional CI smoke target for control-plane regression detection

## Practical deployment recommendation

If deploying now, do this:
1. Put `api`, `postgres`, `nats`, `bridge`, `projector`, and `event-consumer` on Railway.
2. Persist JetStream storage on Railway volume; do not rely on `/tmp` for anything meant to survive restart.
3. Run one lightweight Railway gateway as an always-visible standby.
4. Keep local/mac gateways for privileged/local-tool execution.
5. Do **not** market Warden container respawn on Railway as complete HA until Railway-native respawn exists.

## Bottom line

- **Gateway status visibility:** ready enough to ship
- **Operational failover visibility:** good enough to monitor
- **Cross-gateway Railway redundancy:** incomplete
- **True self-healing failover on Railway:** not done yet
