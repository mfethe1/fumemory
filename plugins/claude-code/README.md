# memU — Karpathy-style LLM wiki for Claude Code

Adds a vault-backed memory system with wiki semantics and an RLM orchestrator that keeps context small by passing **slugs, not bodies**. Works with plain markdown + SQLite for individuals and scales to Postgres + pgvector + Apache AGE for teams.

## Install

```text
/plugin marketplace add mfethe1/fumemory
/plugin install memu
```

## Configure

Set a vault location (defaults to `~/.memu/vault`):

```bash
export MEMU_STORAGE_DSN=file:///path/to/vault     # Tier 0: markdown only
export MEMU_STORAGE_DSN=sqlite:///path/index.db   # Tier 1: + FTS5
export MEMU_STORAGE_DSN=postgres://…              # Tier 2/3: team/enterprise
```

Initialize:

```bash
python -m memu.cli.main init ~/my-vault
```

## Commands

- `/memu:wiki-search <query>` — full-text search, returns ranked slugs.
- `/memu:wiki-write <slug> <body>` — create or update a note.
- `/memu:rlm-investigate <task>` — recursive investigation over the vault.
- `/memu:fix-with-context <symbol>` — pulls the 1-hop neighborhood, proposes a fix.
- `/memu:vault-init <path>` — create a vault directory.

## Subagents

- `wiki-orchestrator` — recursive retrieval + delegation (RLM).
- `wiki-worker` — edits a single slug under a neighborhood lock.

## Hook

`PostToolUse` on `Edit|Write` re-indexes any file in the vault so wikilinks and backlinks stay fresh.

## Obsidian

The vault is plain markdown with YAML frontmatter and `[[wikilinks]]`. Open the folder directly in Obsidian for graph view + backlinks pane.
