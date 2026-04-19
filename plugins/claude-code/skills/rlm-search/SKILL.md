---
name: rlm-search
description: Use when a user task mentions a symbol, file, paper, or concept that is unfamiliar in the current session — especially "fix", "refactor", "where is", "find the", "how does". Calls the memU MCP server's rlm_solve tool to locate relevant slugs without loading files into context.
---

# When to activate

Activate this skill automatically when the user asks to:

- fix / refactor / debug a symbol or module you have not yet seen
- explain how something works across more than one file
- cross-reference a design decision, ADR, or paper
- migrate a pattern that appears in several places

# Workflow

1. Call `mcp__memu__rlm_solve` with `task=<user request>`, `max_depth=2`, `k=6`.
2. Inspect the returned `slugs`. For each relevant slug, call `mcp__memu__wiki_read` with `include_links=true`.
3. If neighbors suggest additional context, call `mcp__memu__wiki_backlinks` on the most-linked node.
4. Summarize findings in ≤200 words before proposing edits.

# Context discipline

The RLM orchestrator is designed so that the main agent only holds **slugs and summaries**, not bodies. Fetch bodies only for slugs you will actually modify. If you are about to load >5 bodies, stop and recurse: call `rlm_solve` again with a narrower task.
