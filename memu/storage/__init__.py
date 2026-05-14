"""Pluggable storage backends for memU: markdown, sqlite, postgres.

The backend is selected via the ``MEMU_STORAGE_DSN`` environment variable:

    file:///~/vault          -> MarkdownBackend (Tier 0: solo, no DB)
    sqlite:///path/index.db  -> SqliteBackend   (Tier 1: solo with index)
    postgres://user@host/db  -> PostgresBackend (Tier 2/3: team/enterprise)

All backends implement :class:`StorageBackend`.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from .base import (
    StorageBackend,
    WikiNode,
    SearchHit,
    LinkRecord,
    SlugRecord,
)

__all__ = [
    "StorageBackend",
    "WikiNode",
    "SearchHit",
    "LinkRecord",
    "SlugRecord",
    "get_backend",
]


def get_backend(dsn: str | None = None) -> StorageBackend:
    """Factory: resolve DSN -> backend instance.

    Falls back to ``MEMU_STORAGE_DSN`` env var, then to a markdown vault at
    ``~/.memu/vault``.
    """
    dsn = dsn or os.environ.get("MEMU_STORAGE_DSN") or "file://~/.memu/vault"
    parsed = urlparse(dsn)
    scheme = parsed.scheme or "file"

    if scheme == "file":
        from .markdown_backend import MarkdownBackend

        path = os.path.expanduser(parsed.path or parsed.netloc)
        return MarkdownBackend(root=path)
    if scheme == "sqlite":
        from .sqlite_backend import SqliteBackend

        # Follow the SQLAlchemy convention:
        #   sqlite:///relative/path  -> parsed.path = '/relative/path'   -> relative
        #   sqlite:////abs/path      -> parsed.path = '//abs/path'       -> absolute
        # ``urlparse`` leaves the leading slashes in ``parsed.path``;
        # we strip exactly one to recover the intended path.
        raw = parsed.path or ""
        if raw.startswith("//"):
            raw = raw[1:]  # keep the leading slash -> absolute
        elif raw.startswith("/"):
            raw = raw[1:]  # drop the scheme's own slash -> relative
        path = os.path.expanduser(raw or ":memory:")
        return SqliteBackend(path=path)
    if scheme in {"postgres", "postgresql"}:
        from .postgres_backend import PostgresBackend

        return PostgresBackend(dsn=dsn)
    raise ValueError(f"Unsupported storage DSN scheme: {scheme!r}")
