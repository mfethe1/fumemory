---
description: Create or update a memU wiki node at a given slug.
allowed-tools: mcp__memu__wiki_read, mcp__memu__wiki_write
argument-hint: <slug> [body...]
---

Write a wiki node.

Input: `$ARGUMENTS`. The first whitespace-separated token is the slug; the rest is the body.

1. Call `mcp__memu__wiki_read` with `ref=<slug>` to see whether the node exists.
2. Call `mcp__memu__wiki_write` with the slug and body.
   - Preserve existing tags if the node already exists.
   - Use `kind="note"` unless the slug begins with `code/`, `paper/`, or `task/`.
3. Confirm by printing the returned id and outbound link count.

If the body contains `[[wikilinks]]`, they are parsed and recorded automatically.
