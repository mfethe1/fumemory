---
description: Run the Karpathy-style RLM orchestrator on a task and report cited slugs.
allowed-tools: mcp__memu__rlm_solve, mcp__memu__wiki_read
argument-hint: <task>
---

Investigate: $ARGUMENTS

1. Call `mcp__memu__rlm_solve` with `task="$ARGUMENTS"`, `max_depth=2`, `k=6`.
2. Print the returned `slugs` list and `answer`.
3. For each of the top 3 cited slugs, call `mcp__memu__wiki_read` and summarize in one line.
4. Propose a short plan. Do **not** modify any files yet — plan first.
