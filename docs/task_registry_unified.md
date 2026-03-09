# Unified Task Registry (Risk + Refinement + Review + NATS)

## Goal

Replace scattered TODO/task trackers with a single SQL-backed registry in memU, with:

- deterministic RLM heartbeat scans
- strict task refinement checks
- risk-first execution policy (`risk > 80` requires manual ask/review)
- explicit completion review loop
- optional GitHub Issue mirroring

## Data model additions

Migration `memu/migrations/013_task_refinement_and_risk_registry.sql` adds:

- `risk_score` (`0..100`)
- `source`, `source_ref`, `project`
- `completion_criteria`
- `review_status`, `reviewer_id`, `reviewed_at`, `review_notes`
- `retry_count`, `refine_status`, `refined_at`
- `source_fingerprint`, `menu_bucket`

## Scanner flow (heartbeat)

Run:

```bash
python3 scripts/task_registry_scanner.py \
  --repos "/Users/harrisonfethe/.openclaw/workspace,/Users/harrisonfethe/Projects" \
  --owner rosie \
  --menu-bucket code-scan \
  --tenant-id "$TASK_TENANT_ID" \
  --github-sync --github-repo mfethe1/fumemory
```

The scanner:

1. finds marker lines using regex (`TODO|FIXME|HACK|XXX|BLOCKED`)
2. computes risk score
3. sets status:
   - `pending` if `risk <= 80` (proceed)
   - `blocked` if `risk > 80` (ask)
4. writes tasks into `backlog` with dedupe by `source_ref`/`source_fingerprint`
5. publishes NATS `swarm.task.drafted` events

## Refinement agent

```bash
python3 scripts/task_refiner_agent.py --limit 200 --publish-nats
```

Runs strict checks for actionability + measurable output, then marks each task as:

- `refine_status=approved`
- `refine_status=needs_revision` (with `review_status=needs_input`)

## Completion reviewer

```bash
python3 scripts/task_completion_reviewer.py \
  --task-id <TASK_UUID> \
  --reviewer-id rosie \
  --result-json '{"outcome":"Fixed function and added tests","evidence":"tests pass in ...","files_touched":["src/x.py"],"tests":["tests/y.py"]}'
```

If completion is not verifiable, task is re-queued for another pass with:
`status=pending`, `review_status=needs_input`, `retry_count +1`.

## API review path

- `POST /tasks/{id}/review` accepts decisions from the system reviewer:
  `approve`, `needs_info`, `rework`, `block`.

## Risk policy

This registry uses inverted confidence semantics:

- **risk <= 80**: auto-proceed into pending flow
- **risk > 80**: treated as blocked and routed for explicit human/agent review


## Cycle execution helper

Use:

```bash
TASK_REGISTRY_REPOS="/Users/harrisonfethe/.openclaw/workspace,/Users/harrisonfethe/Projects" TASK_REGISTRY_DB_URL="$DATABASE_URL" TASK_REGISTRY_RUN_REFINER=1 TASK_REGISTRY_REFINER_PUBLISH_NATS=1 TASK_REGISTRY_PUBLISH_NATS=1 ./scripts/run_task_registry_cycle.sh
```

Environment knobs:
- `TASK_REGISTRY_REPOS` (required): comma-separated repo roots to scan
- `TASK_REGISTRY_DB_URL` / `DATABASE_URL` (required): DB URL for memU
- `TASK_REGISTRY_TENANT_ID` (default: `00000000-0000-0000-0000-000000000001`)
- `TASK_REGISTRY_MENU_BUCKET` (default: `code-scan`)
- `TASK_REGISTRY_OWNER` (default: `system`)
- `TASK_REGISTRY_RUN_REFINER` (default: `1`)
- `TASK_REGISTRY_REFINER_LIMIT` (default: `200`)
- `TASK_REGISTRY_REFINER_PUBLISH_NATS` (default: `0`)
- `TASK_REGISTRY_PUBLISH_NATS` (default: `0`)
- `TASK_REGISTRY_GITHUB_SYNC` + `TASK_REGISTRY_GITHUB_REPO`
