"""MCP prompt templates surfaced as slash-commands in supporting editors.

Claude Code renders these as ``/mcp__memu__<name>``. Cursor, Zed, Continue,
and Windsurf expose them via their MCP prompt menus.
"""
from __future__ import annotations

from typing import Any


PROMPTS: list[dict[str, Any]] = [
    {
        "name": "wiki-bootstrap",
        "description": "Initialize a memU vault and seed it with a project overview.",
        "arguments": [
            {"name": "path", "description": "Vault directory", "required": True},
        ],
    },
    {
        "name": "rlm-investigate",
        "description": "Investigate a task end-to-end using the recursive orchestrator.",
        "arguments": [
            {"name": "task", "description": "Task description", "required": True},
        ],
    },
    {
        "name": "fix-with-context",
        "description": (
            "Locate the right wiki node for a symbol, pull its 1-hop neighborhood, "
            "and propose a fix."
        ),
        "arguments": [
            {"name": "symbol", "description": "Function/class/module name", "required": True},
        ],
    },
]


def render(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the message list for a prompt. Format is MCP-standard."""
    if name == "wiki-bootstrap":
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Initialize a memU vault at {args.get('path', '~/.memu/vault')}. "
                        "Use the `wiki_write` tool to create a master index note at "
                        "slug `master` that summarizes this repository: top-level "
                        "modules, key entry points, and open questions. Cite code "
                        "symbols as `[[code/<module>/<symbol>]]` even if the nodes "
                        "don't exist yet — they'll be materialized on first ingest."
                    ),
                },
            }
        ]
    if name == "rlm-investigate":
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Run `rlm_solve` on: {args['task']!r}. Read the cited slugs "
                        "with `wiki_read`, then synthesize a short plan. Do not "
                        "write code yet — I want to review the plan first."
                    ),
                },
            }
        ]
    if name == "fix-with-context":
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Call `wiki_search` with query {args['symbol']!r} and `kind=\"code\"`. "
                        "Pick the best hit, read it with `wiki_read` (include_links=true), "
                        "walk 1-hop neighbors, then propose a patch. If multiple nodes "
                        "are relevant, call `rlm_solve` to consolidate."
                    ),
                },
            }
        ]
    raise ValueError(f"Unknown prompt: {name!r}")
