"""Obsidian-compatible wiki layer: frontmatter, wikilinks, vault layout.

This package turns a folder of markdown files into a queryable knowledge
graph that any Obsidian user can open natively. memU reads the same files.
"""
from .frontmatter import dump_frontmatter, parse_frontmatter
from .vault import VaultLayout
from .wikilink import WikiLink, parse_wikilinks

__all__ = [
    "VaultLayout",
    "WikiLink",
    "parse_wikilinks",
    "parse_frontmatter",
    "dump_frontmatter",
]
