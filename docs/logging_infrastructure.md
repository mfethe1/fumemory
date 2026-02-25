# Logging Infrastructure (memU/fumemory)

## Structured JSONL logs
- Per-event: `data/logs/<event>.jsonl`
- Unified stream: `data/logs/all.jsonl`
- Python library: `memu/structured_logging.py`
- CLI emitters:
  - Bash: `scripts/log_event.sh`
  - Node: `scripts/log_event.mjs`

## Log viewer CLI
`python3 scripts/log_view.py --event <name> --level <level> --contains <text> --from-ts <ISO> --to-ts <ISO> [--json]`

## Nightly ingest
`python3 scripts/log_ingest.py --log-dir data/logs --db data/logs/log_ingest.db --raw-glob "../.openclaw/logs/*.log"`
- Writes to:
  - `structured_logs`
  - `server_raw_logs`
- Deduped by row hash (`INSERT OR IGNORE`)

## Rotation
`python3 scripts/log_rotate.py --log-dir data/logs --max-mb 50 --keep 3`
- Rotates oversized JSONL files to gzip archives
- Archives SQLite snapshot monthly under `data/logs/archive/`

## Restore/backup relation
This logging layer is included by workspace backup scripts via `*.jsonl` discovery and manifest mapping.
