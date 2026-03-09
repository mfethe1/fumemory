# Gateway Lease Failover Plan

## Goal
Reduce token waste in multi-gateway group chats without creating a single point of failure.

## Approach
- Use **topic/chat ownership leases** stored in memU/Postgres.
- Ownership is **exclusive while healthy** and **transferable on expiry**.
- Gateways renew leases with heartbeats.
- Any healthy backup can atomically acquire an expired lease.
- NATS broadcasts lease changes for observability and fast convergence.

## Data Model
`gateway_topic_leases`
- `lease_key` (`channel:chat:topic` or equivalent)
- `owner_gateway`
- `backup_gateway`
- `lease_expires_at`
- `last_message_id`
- `last_reply_id`
- `context_digest`
- `task_state`
- `version`
- timestamps

## Core Flows
1. **Acquire**
   - create new lease if none exists
   - renew in-place if caller already owns it
   - steal only if expired
2. **Heartbeat / Renew**
   - owner extends `lease_expires_at`
   - optional checkpoint updates: `last_message_id`, `context_digest`, `task_state`
3. **Release**
   - owner clears lease early when done
4. **Read status**
   - anyone can inspect current owner / expiry / backup

## Safety Properties
- Owner changes are monotonic via `version`.
- Takeover only succeeds when lease is expired.
- Release only succeeds for current owner.
- Renew only succeeds for current owner.
- Shared checkpoint data reduces reprocessing after failover.

## NATS Subjects
- `swarm.gateway.lease.claimed`
- `swarm.gateway.lease.renewed`
- `swarm.gateway.lease.released`
- `swarm.gateway.lease.stolen`

## Initial MVP
- Postgres migration
- FastAPI endpoints
- lightweight tests for acquire / renew / takeover / release
- NATS publish on state change

## Next Step After MVP
Hook OpenClaw gateway chat-routing to consult this lease API before replying in shared group chats.
