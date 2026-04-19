"""memU Model Context Protocol server.

Exposes the wiki + RLM capabilities over MCP so Claude Code, Cursor, Zed,
Continue, and any other MCP-aware agent can use memU without per-editor
custom code. The same server powers the Claude Code plugin shipped under
``plugins/claude-code/``.
"""
from .server import run_stdio

__all__ = ["run_stdio"]
