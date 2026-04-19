"""In-process neighborhood lock for wiki_write read-modify-write safety.

The plugin docs promise ``wiki-worker`` agents "edit under a neighborhood
lock." This module backs that promise for the single-process MCP server. A
pluggable :class:`LockBackend` interface lets distributed deployments swap in
a NATS KV or Postgres advisory-lock backend without touching the dispatch
site — see ``memu/lane_lock.py`` for the distributed pattern.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Protocol


class LockBackend(Protocol):
    async def acquire(self, key: str) -> None: ...
    async def release(self, key: str) -> None: ...


class InProcessLockBackend:
    """Per-key asyncio.Lock registry. Safe within one event loop."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def acquire(self, key: str) -> None:
        async with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
        await lock.acquire()

    async def release(self, key: str) -> None:
        lock = self._locks.get(key)
        if lock is not None and lock.locked():
            lock.release()


class NeighborhoodLock:
    """Async context manager acquiring per-slug locks in sorted order.

    Sorted acquisition is load-bearing: every caller takes locks in the same
    order, ruling out the classic two-lock deadlock where worker A holds
    X/wants Y while worker B holds Y/wants X.
    """

    def __init__(self, backend: LockBackend, slugs: Iterable[str]) -> None:
        self._backend = backend
        self._slugs = sorted({s for s in slugs if s})
        self._acquired: list[str] = []

    async def __aenter__(self) -> "NeighborhoodLock":
        for slug in self._slugs:
            await self._backend.acquire(slug)
            self._acquired.append(slug)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        while self._acquired:
            slug = self._acquired.pop()
            await self._backend.release(slug)
