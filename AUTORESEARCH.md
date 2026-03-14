# AUTORESEARCH.md

## Mission
Run a repo-local, low-risk improvement loop for fumemory that turns reliability findings into verified changes or explicit backlog items. Over the next 2-4 weeks the loop should improve three things in order: NATS resilience under partial outage, memU API contract alignment across code/docs/tests, and the quality of the project's own self-improvement plumbing so research produces shippable follow-up work instead of dead notes.

## Scope for This Repo
The loop is intentionally narrow. It is for:
- NATS startup/failover/degraded-mode reliability
- memU contract alignment (`X-MemU-Key` canonical header, legacy `X-API-Key` compatibility, deploy-time env assumptions)
- self-improvement mechanics that are already inside the repo: queue manifests, experiment ledger, and backlog bridging

It is not a general product roadmap or broad architecture rewrite.

## Guardrails
- Keep every iteration small, measurable, and reversible.
- One queued hypothesis at a time (`max_parallel: 1`) until the baseline is stable.
- Prefer test additions, config hardening, and docs/code contract alignment over feature work.
- Do not change billing, production infra, auth policy, or secret handling from this loop.
- Do not remove legacy compatibility unless tests/docs are updated in the same iteration.
- If a change weakens the current baseline, revert it and log the failed experiment in the ledger.

## Priority Order
1. **NATS reliability** — startup assumptions, fallback behavior, degraded mode, outbox/DLQ safety
2. **memU contract alignment** — auth headers, env contract, deploy docs, verification scripts
3. **Loop quality** — make follow-up work easier to queue, score, and ship
4. Refactors only when they reduce immediate operational risk

## Score Function
Each iteration is scored 0-10 and recorded in `autoresearch/ledger/experiments.jsonl`.

### A. Reliability score (0-4)
- +2 if targeted NATS/degraded-mode verification passes
- +1 if failure handling becomes more explicit or test-covered
- +1 if blast radius or operator ambiguity is reduced

### B. Contract score (0-3)
- +1 if docs, code, and tests agree on the same memU API behavior
- +1 if backward compatibility is preserved or intentionally removed with proof
- +1 if deploy/verification scripts encode the documented contract

### C. Loop score (0-3)
- +1 if the queue/ledger/backlog bridge is kept current
- +1 if the iteration leaves a clear next action with evidence
- +1 if rollback criteria are explicit and easy to apply

## Keep / Revert Rule
Keep a change only when:
1. the verification command passes,
2. no previously-covered contract regresses, and
3. at least one score bucket improves without dropping reliability below baseline.

Otherwise revert and append the failed attempt to the ledger with the suspected root cause.

## Evaluation Window
- **Immediate gate:** same branch/session verification must pass before keeping the change.
- **Short window:** the next local or CI verification run should still pass without extra manual steps.

## Verification Commands
Use the smallest command that proves the iteration.

Baseline set for this repo:
```bash
python3 -m pytest -q   tests/test_auth_header_contract.py   tests/test_cluster_startup_assumptions.py   tests/test_memu_auto_write_nats_fallback.py
```

Optional broader check when touching runtime logic:
```bash
python3 -m compileall memu scripts
```

## Queue + Ledger Conventions
- Queue manifests live in `autoresearch/queue/*.json`.
- The append-only experiment log lives at `autoresearch/ledger/experiments.jsonl`.
- Reports or short findings can live in `reports/autoresearch/`.
- Validated outcomes should be bridged into `BACKLOG_AUTO.md` with owner, evidence, and next action.

## Starter Targets
1. Verify NATS fallback and cluster startup assumptions stay green.
2. Keep auth-header compatibility explicit in tests/docs.
3. Convert recurring reliability findings into concrete backlog items instead of leaving them in ad hoc notes.

## Out of Scope
- Large rewrites of the NATS cluster manager
- New orchestration platforms or persistent schedulers
- Production credential changes
- Multi-day migration work without a written plan
