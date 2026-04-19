---
description: Initialize a memU vault directory and set MEMU_STORAGE_DSN for this project.
allowed-tools: Bash(memu init:*), Bash(python -m memu.cli.main init:*)
argument-hint: <path>
---

Initialize a memU vault at `$ARGUMENTS`:

1. Run `python -m memu.cli.main init "$ARGUMENTS"` (falls back to `memu init "$ARGUMENTS"` if the CLI is installed).
2. Tell the user to set `MEMU_STORAGE_DSN=file://$ARGUMENTS` in their shell or project `.env`.
3. Remind them they can open the folder directly in Obsidian — memU writes vanilla markdown with YAML frontmatter.
