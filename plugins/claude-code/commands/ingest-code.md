---
description: Ingest a source tree into the memU wiki so every symbol becomes an addressable slug.
allowed-tools: mcp__memu__wiki_ingest_code, mcp__memu__wiki_search, mcp__memu__wiki_read
argument-hint: <path> [--exclude <glob>]*
---

Ingest the codebase at `$ARGUMENTS`:

1. Parse the arguments. The first whitespace-separated token is the source root. Any subsequent `--exclude <glob>` arguments collect into a list.
2. Call `mcp__memu__wiki_ingest_code` with:
   - `path` = the source root
   - `languages` = `["python"]` (the only language memU parses today)
   - `excludes` = the collected list (plus `"tests/**"` by default if no excludes were passed)
   - `respect_gitignore` = `true`
3. Report the summary: `added`, `updated`, `unchanged_count`, `scanned_files`, `elapsed_ms`, `commit_sha`.
4. Pick one of the newly added slugs and call `mcp__memu__wiki_read` to show the user what a freshly ingested code node looks like. Highlight:
   - the frontmatter `source.path` / `source.symbol` / `source.commit`
   - outbound `[[wikilinks]]` to imports and callees

Tell the user they can now use `/rlm-investigate` or `/fix-with-context` against any symbol they just ingested, and that the vault is Obsidian-openable.
