---
name: vault-doctor
description: Use for memU vault hygiene — orphaned wikilinks, dangling slugs, stub nodes with confidence=0, broken backlinks. Trigger phrases include "vault health", "fix broken links", "which notes have no incoming links", "clean up the vault".
---

# What to do

1. Call `mcp__memu__wiki_search` with an empty-ish query (e.g. the user's issue) to sample the vault, or use the `memu` CLI directly: `memu links <slug>` and `memu search ""` via Bash.
2. For each suspect slug, call `mcp__memu__wiki_read`:
   - `confidence == 0` and `memory_type == placeholder` → stub waiting for real content.
   - No inbound backlinks (`mcp__memu__wiki_backlinks` returns empty `inbound`) → orphan.
3. Report a triage list, never delete without confirmation.

# Repair playbook

- Stub with clear intent: promote via `mcp__memu__wiki_write` with the real body + `kind`.
- True orphan with no owner: ask the user before removing.
- Broken link (`[[foo]]` where `foo` is not registered): either create the stub via `wiki_write` or rewrite the link to an existing slug.
