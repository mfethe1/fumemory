# Gateway Federation Rollout Checklist

Status: **operator runbook**  
Owner: Macklemore  
Last updated: 2026-03-25

This file is the practical rollout companion to:
- `docs/CROSS_GATEWAY_NATS_FEDERATION.md`
- `scripts/gateway_federation_smoke.py`
- `memu/policy/rules/jetstream_authz.rego`

Use this to bring each gateway onto the shared Railway-backed NATS federation **one machine at a time**.

---

## Goal

A gateway is considered **federation-ready** only if all are true:

1. latest `fumemory` main is pulled
2. required env vars are present
3. `opa` is available on-host, or an equivalent policy runtime exists
4. `scripts/gateway_federation_smoke.py --skip-memu --json` passes
5. full `scripts/gateway_federation_smoke.py --json` passes
6. `opa check memu/policy/rules/jetstream_authz.rego` passes

---

## Required environment contract

Every gateway must have these values available before it can join shared dispatch.

### Required for NATS federation
- `GATEWAY_ID`
- `NATS_RAILWAY_URL`

### Required when Railway NATS auth is enabled
- `NATS_AUTH_TOKEN`

### Required for full memU-linked verification
- `MEMU_BASE_URL` or equivalent memU base setting
- `X_MEMU_KEY` or equivalent memU key setting

### Strongly recommended
- `MEMU_API_KEY` compatibility key
- stable durable consumer naming derived from `GATEWAY_ID`

---

## Readiness commands

Run on each gateway from the `fumemory` repo root.

### 1. Pull latest

```bash
git checkout main
git pull --rebase origin main
```

### 2. Prove artifacts are present

```bash
test -f docs/CROSS_GATEWAY_NATS_FEDERATION.md
test -f docs/GATEWAY_FEDERATION_ROLLOUT_CHECKLIST.md
test -f scripts/gateway_federation_smoke.py
test -f memu/policy/rules/jetstream_authz.rego
```

### 3. Unit proof

```bash
python -m pytest tests/test_gateway_federation.py -q
```

### 4. NATS-only smoke

```bash
python scripts/gateway_federation_smoke.py --skip-memu --json
```

### 5. Full smoke

```bash
python scripts/gateway_federation_smoke.py --json
```

### 6. Policy proof

```bash
opa check memu/policy/rules/jetstream_authz.rego
```

---

## Failure interpretation

### If NATS-only smoke fails with `NATS_RAILWAY_URL is required`
The gateway is missing:
- `NATS_RAILWAY_URL`

It may also still need:
- `GATEWAY_ID` if not set explicitly and hostname fallback is not acceptable
- `NATS_AUTH_TOKEN` if the Railway broker is authenticated

### If full smoke fails after NATS-only passes
The gateway is missing memU contract wiring:
- `MEMU_BASE_URL` and/or
- `X_MEMU_KEY` / `MEMU_API_KEY`

### If `opa check` fails because `opa` is not installed
The gateway is blocked on policy runtime availability.

### If Docker fallback is mentioned but Docker is down
Do not treat that as a valid workaround. Bring up on-host `opa` or Docker first.

---

## Per-gateway status board

Update this section as each machine is verified.

| Gateway | Repo @ main | NATS env | memU env | OPA runtime | NATS-only smoke | Full smoke | Status | Notes |
|---|---:|---:|---:|---:|---:|---:|---|---|
| Mac golden reference | ✅ | pending live proof | pending live proof | ✅ on authoring host | pending | pending | in_progress | Code and verifier landed; use as baseline |
| Rosie node | ✅ | ❌ missing `NATS_RAILWAY_URL` | unknown | ❌ `opa` missing | blocked | blocked | blocked | Reported: tests pass, verifier help works, NATS env missing, Docker down |
| Winnie node | unknown | unknown | unknown | unknown | unknown | unknown | pending | Needs check-in |
| Lenny node | unknown | unknown | unknown | unknown | unknown | unknown | pending | Needs check-in |
| Windows node | unknown | unknown | unknown | unknown | unknown | unknown | pending | Roll last once reachable |

---

## Exact checklist by gateway

## 1) Mac golden reference

Expected role:
- first live proof against Railway NATS
- source-of-truth for env shape and command sequence

Checklist:
- [ ] `git pull --rebase origin main`
- [ ] confirm `GATEWAY_ID`
- [ ] confirm `NATS_RAILWAY_URL`
- [ ] confirm `NATS_AUTH_TOKEN` if broker requires auth
- [ ] confirm `MEMU_BASE_URL`
- [ ] confirm `X_MEMU_KEY`
- [ ] run NATS-only smoke
- [ ] run full smoke
- [ ] run `opa check`
- [ ] save proof artifact output

Still needed:
- live execution proof against Railway NATS

## 2) Rosie node

Known current state:
- repo updated to `main`
- federation artifacts present
- `python -m pytest tests/test_gateway_federation.py -q` passed
- `python scripts/gateway_federation_smoke.py --help` passed
- `python scripts/gateway_federation_smoke.py --skip-memu --json` blocked because `NATS_RAILWAY_URL` missing
- `opa check memu/policy/rules/jetstream_authz.rego` blocked because `opa` not installed
- Docker fallback also unavailable because daemon is down

Exact missing items on Rosie node right now:
- `NATS_RAILWAY_URL`
- `opa` runtime

Potentially still needed after that:
- `NATS_AUTH_TOKEN` if Railway NATS requires auth
- `MEMU_BASE_URL`
- `X_MEMU_KEY`

Checklist:
- [ ] set `NATS_RAILWAY_URL`
- [ ] set `NATS_AUTH_TOKEN` if required
- [ ] install `opa` or bring Docker up
- [ ] run NATS-only smoke
- [ ] set/verify memU env
- [ ] run full smoke
- [ ] run `opa check`

## 3) Winnie node

Status:
- no verified report yet

Checklist:
- [ ] pull latest `main`
- [ ] confirm federation artifacts present
- [ ] run unit proof
- [ ] check `GATEWAY_ID`
- [ ] check `NATS_RAILWAY_URL`
- [ ] check `NATS_AUTH_TOKEN`
- [ ] check `MEMU_BASE_URL`
- [ ] check `X_MEMU_KEY`
- [ ] run NATS-only smoke
- [ ] run full smoke
- [ ] run `opa check`

## 4) Lenny node

Status:
- no verified report yet

Checklist:
- [ ] pull latest `main`
- [ ] confirm federation artifacts present
- [ ] run unit proof
- [ ] check `GATEWAY_ID`
- [ ] check `NATS_RAILWAY_URL`
- [ ] check `NATS_AUTH_TOKEN`
- [ ] check `MEMU_BASE_URL`
- [ ] check `X_MEMU_KEY`
- [ ] run NATS-only smoke
- [ ] run full smoke
- [ ] run `opa check`

## 5) Windows node

Status:
- roll last
- do not attempt until reachability is stable

Checklist:
- [ ] confirm node reachable
- [ ] pull latest `main`
- [ ] confirm federation artifacts present
- [ ] check `GATEWAY_ID`
- [ ] check `NATS_RAILWAY_URL`
- [ ] check `NATS_AUTH_TOKEN`
- [ ] check `MEMU_BASE_URL`
- [ ] check `X_MEMU_KEY`
- [ ] ensure `opa` available or equivalent policy runtime
- [ ] run NATS-only smoke
- [ ] run full smoke
- [ ] run `opa check`

---

## Recommended rollout order

1. Mac golden reference
2. Rosie node
3. Winnie node
4. Lenny node
5. Windows node last

Reason:
- validate one good path first
- fix env/runtime issues on the first blocked host
- reuse the same exact proof pattern for the rest

---

## Operator note

If a gateway says “tests pass” but cannot run the smoke verifier, that gateway is **not ready**.

The real gate is not repo sync. The real gate is:
- Railway NATS connectivity
- memU-linked verification
- policy runtime availability

Until those pass, the gateway remains **code-ready only**.
