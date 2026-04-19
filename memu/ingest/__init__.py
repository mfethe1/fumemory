"""Ingest external sources (codebases, papers) into the memU wiki.

This package turns source trees and document libraries into addressable
wiki nodes. Each ingested symbol or document becomes one markdown file
with a stable slug (``code/<module>/<symbol>``, ``paper/<arxiv-id>``),
typed outbound links, and enough frontmatter for later edits to stay
incremental.

Public API::

    from memu.ingest import ingest_codebase, IngestResult

    result = await ingest_codebase(backend, Path("./src"))
    print(result.summary())
"""
from .codebase import IngestResult, ingest_codebase

__all__ = ["ingest_codebase", "IngestResult"]
