---
description: Pull the wiki neighborhood for a symbol, then propose a fix.
allowed-tools: mcp__memu__wiki_search, mcp__memu__wiki_read, mcp__memu__wiki_backlinks, mcp__memu__rlm_solve
argument-hint: <symbol-or-concept>
---

Fix work for: $ARGUMENTS

1. `mcp__memu__wiki_search` with `query="$ARGUMENTS"`, `kind="code"`, `k=5`.
2. Pick the best hit. Call `mcp__memu__wiki_read` with `include_links=true`.
3. Call `mcp__memu__wiki_backlinks` on that slug to see who depends on it.
4. If more than 3 distinct related slugs appear, call `mcp__memu__rlm_solve` with `task="fix $ARGUMENTS"` to get a consolidated view — this avoids loading every body.
5. Propose a patch. Reference slugs with `[[slug]]` syntax in your plan so later agents can follow the trail.
