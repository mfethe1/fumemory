---
description: Reindex the memU vault on disk into the active index backend so searches and backlinks stay fresh.
allowed-tools: Bash(python -m memu.cli.main sync:*)
argument-hint: [path] [--watch]
---

Sync the memU vault.

1. Parse `$ARGUMENTS`. If the first token is a path, use it; otherwise default to `~/.memu/vault` or whatever `MEMU_STORAGE_DSN` points at.
2. If `--watch` is among the arguments, run the long-running daemon: `python -m memu.cli.main sync watch <path>`. Explain to the user that it polls every second and coalesces bursts (so Obsidian autosave doesn't thrash the index).
3. Otherwise run the one-shot reindex: `python -m memu.cli.main sync run <path>`. Report the summary (added / updated / unchanged / scanned / elapsed).
4. Mention that the daemon is filesystem → backend only — it reads your vault but never overwrites your markdown. You can keep editing in Obsidian, VS Code, or anything else and the index will follow.
