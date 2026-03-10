# Cross-Gateway Status + Sync Contract

Purpose: keep hybrid memory/state coordination event-driven, retained, and visible enough that Macklemore can catch regressions before cron sweeps do.

## Retained state plane

JetStream KV bucket:
- `GATEWAY_STATE`

Each gateway writes its latest retained snapshot under key = `gateway_id`.

Minimum retained fields:
- `gateway_id`
- `status` (`online | degraded | offline | draining`)
- `current_task_id` (nullable)
- `last_heartbeat_at`
- `active_nats_node` (`local | railway | standalone`)
- `sync_revision`
- `metadata`

## Real-time event plane

Subjects:
- `swarm.gateway.register` — gateway boot / capability refresh
- `swarm.gateway.heartbeat` — retained-state refresh, failover note, task-bearing pulse
- `swarm.gateway.status` — explicit lifecycle changes (`draining`, `offline`, failover state changes)

Back-compat subjects still emitted/consumed:
- `swarm.discovery`
- `swarm.warden.heartbeat`

## Behavioral rules

1. **Warm standby still pulses retained status**
   - standby gateways must remain visible as `online`
   - only task-specific execution heartbeats remain task-bound

2. **Status is retained, not inferred from cron alone**
   - readers should prefer `GATEWAY_STATE` as source of truth
   - event subjects are for live fan-out + observability

3. **Staleness policy**
   - `>= 20s` without heartbeat => effective status `degraded`
   - `>= 60s` without heartbeat => effective status `offline`
   - explicit `draining` / `offline` overrides staleness mapping

4. **Failover visibility**
   - every retained snapshot includes `active_nats_node`
   - operational views should alert when active node != `local`

## Operational endpoints

- `GET /api/v1/gateways/status` — auth-gated API view for automation / Macklemore
- `GET /api/gateways/status` — Glass Box bridge view for dashboard consumers

Both views should include:
- `summary`
- `gateways[]`
- `alerts[]`
- `cluster`

## Why this replaces cron-heavy coupling

Before:
- standby gateways often disappeared because only task-bearing loops heartbeated
- health depended on external polling / cron interpretation
- cluster failover state was hard to correlate with gateway freshness

Now:
- gateways self-report via JetStream-backed retained state
- real-time NATS subjects update dashboards immediately
- Macklemore gets a stable, queryable mesh overview with explicit alert codes
