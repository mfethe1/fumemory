"""Hook entrypoints invoked by editor plugins (Claude Code PostToolUse, etc.).

These scripts read a small JSON payload from stdin (what the editor passes
to hooks) and update the memU vault accordingly. They exit 0 even on
recoverable errors so they never block the editor.
"""
