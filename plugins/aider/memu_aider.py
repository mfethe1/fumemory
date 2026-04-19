"""Aider integration: inject memU wiki search results as repo-map context.

Drop this file into your Aider config directory and reference it from the
``--read`` hook, or import :func:`augment_repo_map` directly.

Aider does not speak MCP today, so we call the memU dispatcher in-process.
The same dispatcher powers the MCP server, so behavior is identical.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

from memu.mcp import tools
from memu.storage import get_backend


async def _search(query: str, k: int = 6) -> list[dict]:
    backend = get_backend()
    await backend.init()
    try:
        result = await tools.dispatch(backend, "wiki_search", {"query": query, "k": k})
    finally:
        await backend.close()
    return result.get("hits", [])


def augment_repo_map(prompt_text: str, k: int = 6) -> str:
    """Return ``prompt_text`` with a ``## memU context`` section appended.

    Aider calls this before sending the prompt to the LLM so the model sees
    the top-k wiki slugs for the user's request. Only snippets are injected;
    bodies are fetched lazily via ``mcp__memu__wiki_read`` if Aider decides
    to load them.
    """
    hits = asyncio.run(_search(prompt_text, k=k))
    if not hits:
        return prompt_text
    lines = ["", "## memU wiki context", ""]
    for h in hits:
        lines.append(f"- [[{h['slug']}]] ({h['kind']}) — {h['title']}")
        snippet = (h.get("snippet") or "").replace("\n", " ").strip()
        if snippet:
            lines.append(f"  > {snippet[:240]}")
    return prompt_text + "\n".join(lines) + "\n"


__all__ = ["augment_repo_map"]
