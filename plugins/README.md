# memU coding-agent plugins

memU exposes a single MCP server (`python -m memu.mcp.server`). Each editor's
"plugin" is just a config file that points at that server, so adding a new
editor is one manifest — not N adapters.

| Editor / agent | Config | How to install |
|---|---|---|
| Claude Code | `claude-code/` (full plugin bundle) | `/plugin marketplace add mfethe1/fumemory && /plugin install memu` |
| Cursor | `cursor/mcp.json` | Copy to `~/.cursor/mcp.json` (or project-local `.cursor/mcp.json`) |
| Zed | `zed/settings.json` | Merge into `~/.config/zed/settings.json` under `context_servers` |
| Continue | `continue/config.json` | Merge into `~/.continue/config.json` |
| Windsurf | `windsurf/mcp.json` | Copy to Windsurf's MCP config path |
| Aider | `aider/memu_aider.py` | `import augment_repo_map` from your aider hook config |
| OpenAI Codex CLI | `codex/tools.json` | Load via `--tools codex/tools.json` |
| Gemini CLI | `gemini/tools.json` | Point `GEMINI_TOOL_CONFIG` at it |
| Raw HTTP | `memu/api.py` FastAPI app | Existing memU REST endpoints |
| LangGraph / PydanticAI / CrewAI | `memu/adapters/*` | In-process, no MCP needed |

All entries talk to the same `StorageBackend` and the same `rlm.orchestrator`, so a note written from Cursor is instantly readable in Claude Code, and an edit made in Aider raises the same backlinks as one made via the CLI.

## Backend selection

```bash
export MEMU_STORAGE_DSN=file://~/vault          # Tier 0 — markdown only
export MEMU_STORAGE_DSN=sqlite:///~/vault/.memu/index.db
export MEMU_STORAGE_DSN=postgres://user@host/memu  # Tier 2/3 (Phase 5)
```
