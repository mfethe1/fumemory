"""Vault directory layout.

Conventions::

    vault/
      notes/<slug>.md
      code/<lang>/<module>/<symbol>.md
      papers/<arxiv-id>.md
      tasks/<task-id>.md
      _index/master.md
      _index/backlinks.json
      .memu/index.db          (SQLite, when Tier 1 is enabled)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

NodeKind = Literal["note", "code", "paper", "task"]


@dataclass(frozen=True)
class VaultLayout:
    root: Path

    @property
    def notes(self) -> Path:
        return self.root / "notes"

    @property
    def code(self) -> Path:
        return self.root / "code"

    @property
    def papers(self) -> Path:
        return self.root / "papers"

    @property
    def tasks(self) -> Path:
        return self.root / "tasks"

    @property
    def index(self) -> Path:
        return self.root / "_index"

    @property
    def cache(self) -> Path:
        return self.root / ".memu"

    def ensure(self) -> None:
        for p in (self.notes, self.code, self.papers, self.tasks, self.index, self.cache):
            p.mkdir(parents=True, exist_ok=True)
        master = self.index / "master.md"
        if not master.exists():
            master.write_text(
                "---\nid: _index\nslug: _index/master\nkind: note\ntitle: Master Index\n---\n"
                "# Master Index\n\nThis file is maintained by memU's RLM compactor.\n",
                encoding="utf-8",
            )

    def path_for(self, *, kind: NodeKind, slug: str) -> Path:
        safe = slug.replace("..", "").strip("/")
        if kind == "note":
            return self.notes / f"{safe}.md"
        if kind == "code":
            return self.code / f"{safe}.md"
        if kind == "paper":
            return self.papers / f"{safe}.md"
        if kind == "tasks" or kind == "task":
            return self.tasks / f"{safe}.md"
        raise ValueError(f"Unknown node kind: {kind!r}")
