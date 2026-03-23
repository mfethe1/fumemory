# Railway Memory Release Gate

Owner: Lenny (QA/Guardian)
Date: 2026-03-23

## Goal

Define the minimum evidence required before declaring Railway-backed memU memory robust enough for release.

## Diagnosis Summary

### Root cause of the 31 JetStream API errors

Local JetStream reports `api.errors=31/127` (~24%). The dominant failure mode is **AGENT_EVENTS subject drift**:

- `SessionEventBus` publishes to subject `AGENT_EVENTS`
- the existing `AGENT_EVENTS` stream was configured with subjects:
  - `events.>`
  - `agent.>`
  - `swarm.events`
- result: publishes to `AGENT_EVENTS` can miss stream bindings and fail at the JetStream API layer

The durable consumer also created the stream with `subjects=[self.stream]` instead of `subjects=[self.subject]`, which reinforced the mismatch.

### Code-level fix shipped in this branch

- `memu/agent_events_consumer.py`
  - creates the stream with `subjects=[self.subject]`
  - reconciles existing stream subjects when the requested subject is missing
  - creates the durable consumer with `filter_subject=self.subject`
  - fixes ack accounting so `acked_count` only increments on successful ack

### Remaining deploy dependencies outside this branch

These are still required for true production robustness:

1. Railway NATS auth must be enabled (`NATS_AUTH_TOKEN` set and enforced)
2. `ws_bridge` must be deployed as its own Railway service
3. `NATS_RAILWAY_URL` must be explicit for non-Railway agents; no silent internal-host fallback

## Release Gate

Release is **BLOCKED** until every item below is green.

### G1 — AGENT_EVENTS delivery correctness

- [ ] `tests/test_agent_events_consumer.py` passes
- [ ] `tests/test_agent_events_consumer_integration.py` passes against a live NATS broker
- [ ] JetStream `AGENT_EVENTS` stream subjects include the publish subject in production
- [ ] `acked_count` never increments when ack fails

Verification:

```bash
pytest -q tests/test_agent_events_consumer.py tests/test_agent_events_consumer_integration.py
curl -s "$NATS_MONITOR_URL/jsz?consumers=true&config=true&streams=true"
```

Pass criteria:

- durable consumer processes a valid event end-to-end
- publish subject is present in stream config
- no false-positive acks

### G2 — JetStream API error budget

- [ ] JetStream API error rate < 1% over a representative window
- [ ] No new `AGENT_EVENTS` publish failures after deploy

Verification:

```bash
curl -s "$NATS_MONITOR_URL/jsz?consumers=true&config=true&streams=true"
```

Pass criteria:

- `api.errors / api.total < 0.01`
- sustained for at least 30 minutes after deploy under normal agent traffic

### G3 — Cross-gateway memory write path

- [ ] local gateway can write memory that is visible via Railway-backed search
- [ ] Railway gateway can write memory that is visible to a second gateway
- [ ] state projector stays healthy while this traffic runs

Verification flow:

1. write a unique sentinel memory from gateway A
2. search from gateway B
3. repeat in reverse direction
4. inspect `/health` and `/health/report`

Pass criteria:

- 2/2 sentinel writes retrievable cross-gateway
- projector health remains `healthy`
- no JetStream error spike during test window

### G4 — Railway NATS security

- [ ] `NATS_AUTH_TOKEN` is configured in Railway
- [ ] NATS server config enforces auth
- [ ] unauthenticated connection attempt is rejected

Pass criteria:

- authenticated clients succeed
- unauthenticated clients fail loudly

### G5 — Bridge deployment

- [ ] `ws_bridge` is deployed as a separate Railway service
- [ ] bridge health endpoint is green
- [ ] disconnect/reconnect test recovers without manual restart

Pass criteria:

- bridge survives one forced reconnect cycle
- event forwarding resumes automatically

### G6 — Loud configuration failures

- [ ] non-Railway agents fail loudly when `NATS_RAILWAY_URL` is missing
- [ ] no fallback to `nats.railway.internal` outside Railway network

Pass criteria:

- startup exits with actionable error instead of hanging or silently degrading

## Suggested deploy-order

1. deploy AGENT_EVENTS subject reconciliation + ack fix
2. enable Railway NATS auth
3. deploy `ws_bridge`
4. remove silent Railway hostname fallback
5. run cross-gateway sentinel test
6. hold 30-minute observation window and confirm API errors < 1%

## Ship / No-Ship Rule

- **Ship** only if G1-G6 are all green
- **No-ship** if JetStream API errors stay >= 1%, cross-gateway retrieval is flaky, or Railway NATS is reachable without auth
