---
description: Full-text search the memU wiki and return ranked slugs.
allowed-tools: mcp__memu__wiki_search, mcp__memu__wiki_read
argument-hint: <query>
---

Search the memU vault for "$ARGUMENTS":

1. Call `mcp__memu__wiki_search` with `query="$ARGUMENTS"` and `k=8`.
2. Print the ranked slug list with titles and scores.
3. If the top hit's `kind` is `code` or `paper`, also call `mcp__memu__wiki_read` on it so the user sees the body.

Never invent slugs that are not in the results.
