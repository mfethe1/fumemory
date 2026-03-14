# fumemory autoresearch loop

Minimal repo-local scaffolding for small, reversible improvement cycles.

## Files
- `../AUTORESEARCH.md` — charter, score function, verification rules
- `queue/*.json` — one manifest per candidate experiment
- `ledger/experiments.jsonl` — append-only experiment history
- `../BACKLOG_AUTO.md` — validated follow-up work only
- `../reports/autoresearch/` — optional short findings or summaries

## Operating mode
- Default concurrency: `1`
- Default unit of work: one hypothesis, one verification step, one ledger entry
- Default fallback: revert the change, then record why it failed

## Current focus
1. NATS reliability under partial outage or bad startup config
2. memU contract alignment across code, tests, and docs
3. loop hygiene so research feeds execution instead of accumulating notes
