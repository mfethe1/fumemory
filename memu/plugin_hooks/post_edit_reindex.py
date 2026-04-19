"""PostToolUse hook: re-parse wikilinks and backlinks for files the editor touched.

The hook receives a JSON payload on stdin like::

    {"tool_input": {"file_path": "/abs/path.md"}, ...}

If the path lives inside the active memU vault (``MEMU_STORAGE_DSN=file://...``
or the path happens to contain a ``notes/`` / ``code/`` / ``papers/`` /
``tasks/`` segment), we rebuild that file's outbound links in the backend.
Everything else is a silent no-op. The hook always exits 0 so it cannot
block an editor session.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..storage import get_backend
from ..storage.base import LinkRecord, WikiNode
from ..wiki.frontmatter import parse_frontmatter
from ..wiki.wikilink import parse_wikilinks


def _extract_path(payload: dict) -> Optional[Path]:
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    candidate = tool_input.get("file_path") or tool_input.get("filePath")
    if not candidate:
        return None
    return Path(candidate).expanduser().resolve()


def _vault_root() -> Optional[Path]:
    dsn = os.environ.get("MEMU_STORAGE_DSN", "")
    if dsn.startswith("file://"):
        return Path(dsn[len("file://") :]).expanduser().resolve()
    return None


def _is_vault_file(path: Path, vault: Optional[Path]) -> bool:
    if path.suffix != ".md":
        return False
    if vault is not None:
        try:
            path.relative_to(vault)
            return True
        except ValueError:
            return False
    return any(seg in path.parts for seg in ("notes", "code", "papers", "tasks"))


async def _reindex(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    fm, body = parse_frontmatter(raw)
    slug = fm.get("slug") or path.stem
    node_id = str(fm.get("id") or uuid.uuid4())
    kind = fm.get("kind", "note")
    links = [
        LinkRecord(src_slug=slug, dst_slug=link.slug, type="related")
        for link in parse_wikilinks(body)
    ]
    node = WikiNode(
        id=node_id,
        slug=slug,
        kind=kind,
        title=fm.get("title") or slug,
        body=body,
        tags=fm.get("tags") or [],
        metadata=fm.get("metadata") or {},
        agent_id=fm.get("agent_id", "claude-code"),
        memory_type=fm.get("memory_type", "observation"),
        salience=float(fm.get("salience", 0.5)),
        confidence=float(fm.get("confidence", 1.0)),
        source=fm.get("source"),
        links=links,
        created_at=datetime.now(timezone.utc),
    )
    backend = get_backend()
    await backend.init()
    try:
        await backend.put_node(node)
    finally:
        await backend.close()


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    path = _extract_path(payload)
    if path is None:
        return 0
    vault = _vault_root()
    if not _is_vault_file(path, vault):
        return 0
    try:
        asyncio.run(_reindex(path))
    except Exception:
        # Never surface errors to the editor — hook failures must not
        # interrupt the user's edit flow.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
