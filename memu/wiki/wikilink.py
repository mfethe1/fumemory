"""Obsidian-style wikilink parser: ``[[slug]]``, ``[[slug|alias]]``, ``[[slug#anchor]]``.

Namespaced slugs (``paper/2312.12345``, ``code/foo.py:FooParser``) are
preserved verbatim so the slug registry can dispatch to the right ingestor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Group 1: target (slug, optional namespace). Group 2: #anchor. Group 3: |alias.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\|#]+)(#[^\[\]\|]+)?(\|[^\[\]]+)?\]\]")


@dataclass(frozen=True)
class WikiLink:
    slug: str
    anchor: str | None = None
    alias: str | None = None

    @property
    def namespace(self) -> str | None:
        if "/" in self.slug:
            return self.slug.split("/", 1)[0]
        return None

    def display(self) -> str:
        return self.alias or self.slug


def parse_wikilinks(body: str) -> list[WikiLink]:
    """Extract every wikilink from ``body`` in document order, de-duplicated
    on (slug, anchor, alias)."""
    seen: set[tuple[str, str | None, str | None]] = set()
    out: list[WikiLink] = []
    for m in _WIKILINK_RE.finditer(body):
        slug = m.group(1).strip()
        anchor = m.group(2)[1:].strip() if m.group(2) else None
        alias = m.group(3)[1:].strip() if m.group(3) else None
        key = (slug, anchor, alias)
        if key in seen:
            continue
        seen.add(key)
        out.append(WikiLink(slug=slug, anchor=anchor, alias=alias))
    return out


def iter_wikilinks(bodies: Iterable[str]) -> Iterable[WikiLink]:
    for body in bodies:
        yield from parse_wikilinks(body)
