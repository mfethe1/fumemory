"""Neighborhood locks for wiki-worker agents.

When a ``wiki-worker`` starts editing a slug, it needs exclusive access to
that slug (and optionally a 1-hop neighborhood) so that two agents do not
clobber each other's writes. This module provides the lock registry shared
by both storage tiers plus the context manager agents use at runtime.

Two registries ship in-tree:

- :class:`SqliteLockRegistry` reuses the shared SQLite connection that the
  wiki backend already opens (tables land alongside the nodes/slug tables
  introduced by PR #21). It's the production path for anything that talks
  to :class:`memu.storage.sqlite_backend.SqliteBackend`.
- :class:`MarkdownLockRegistry` keeps Tier 0 fully filesystem-native: locks
  live in ``<vault>/.memu/locks/<slug>.json`` and a monotonic fencing
  counter lives in ``<vault>/.memu/locks/_fence/<slug>``.

Both expose the same :class:`LockRegistry` protocol so
:class:`NeighborhoodLock` can remain backend-agnostic.

``NeighborhoodConflict`` subclasses the existing
:class:`memu.lane_lock.LaneContestedError`, so callers that already trap the
lane-lock hierarchy will catch wiki contention without new except clauses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Protocol, runtime_checkable

from memu.lane_lock import LaneContestedError

logger = logging.getLogger(__name__)

DEFAULT_TTL_S: float = 30.0
DEFAULT_RENEW_INTERVAL_S: float = 10.0


class NeighborhoodConflict(LaneContestedError):
    """Raised when a neighborhood lock cannot be acquired or kept."""


@dataclass(frozen=True)
class LockRecord:
    """Snapshot of a single slug's lock state."""

    slug: str
    holder_id: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime


@runtime_checkable
class LockRegistry(Protocol):
    """Backend-agnostic interface for neighborhood locks."""

    async def init(self) -> None: ...

    async def try_acquire(
        self, slug: str, holder_id: str, ttl_s: float
    ) -> LockRecord:
        """Claim ``slug`` for ``holder_id``; raise :class:`NeighborhoodConflict`
        if a live peer already holds it."""

    async def renew(
        self, slug: str, holder_id: str, fencing_token: int, ttl_s: float
    ) -> LockRecord:
        """Extend the lease. Must fail if someone stole the lock."""

    async def release(
        self, slug: str, holder_id: str, fencing_token: int
    ) -> bool:
        """Drop the lock if still owned; return True on success."""

    async def peek(self, slug: str) -> Optional[LockRecord]:
        """Return the current record without mutating state."""

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SQLite registry
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS neighborhood_locks (
    slug            TEXT PRIMARY KEY,
    holder_id       TEXT NOT NULL,
    fencing_token   INTEGER NOT NULL,
    acquired_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS neighborhood_fence_tokens (
    slug            TEXT PRIMARY KEY,
    current_token   INTEGER NOT NULL DEFAULT 0
);
"""


class SqliteLockRegistry:
    """Lock registry that piggybacks on an existing SQLite connection.

    The backend already holds the connection in WAL mode; we only mutate two
    additional tables. All registry methods run under a single asyncio lock
    to keep read-check-write sequences atomic inside the process.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._mu = asyncio.Lock()

    async def init(self) -> None:
        self._conn.executescript(_SQLITE_SCHEMA)
        self._conn.commit()

    async def try_acquire(
        self, slug: str, holder_id: str, ttl_s: float
    ) -> LockRecord:
        async with self._mu:
            now = datetime.now(timezone.utc)
            row = self._conn.execute(
                "SELECT holder_id, fencing_token, expires_at"
                " FROM neighborhood_locks WHERE slug = ?",
                (slug,),
            ).fetchone()
            if row is not None:
                expires_at = _parse_iso(row[2])
                if expires_at > now:
                    raise NeighborhoodConflict(
                        f"slug {slug!r} held by {row[0]!r} until {row[2]}"
                    )
                # Lease expired -> reclaim by deleting; fence counter keeps
                # advancing so any resumed writer is detectably stale.
                self._conn.execute(
                    "DELETE FROM neighborhood_locks WHERE slug = ?", (slug,)
                )

            new_token = self._bump_fence(slug)
            expires = now + timedelta(seconds=ttl_s)
            self._conn.execute(
                "INSERT INTO neighborhood_locks"
                " (slug, holder_id, fencing_token, acquired_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    slug,
                    holder_id,
                    new_token,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            self._conn.commit()
            return LockRecord(slug, holder_id, new_token, now, expires)

    async def renew(
        self, slug: str, holder_id: str, fencing_token: int, ttl_s: float
    ) -> LockRecord:
        async with self._mu:
            row = self._conn.execute(
                "SELECT holder_id, fencing_token FROM neighborhood_locks"
                " WHERE slug = ?",
                (slug,),
            ).fetchone()
            if row is None:
                raise NeighborhoodConflict(
                    f"slug {slug!r}: lock no longer present"
                )
            if row[0] != holder_id or int(row[1]) != fencing_token:
                raise NeighborhoodConflict(
                    f"slug {slug!r}: fencing token mismatch"
                    f" (held {fencing_token}, current {row[1]})"
                )
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=ttl_s)
            self._conn.execute(
                "UPDATE neighborhood_locks SET expires_at = ? WHERE slug = ?",
                (expires.isoformat(), slug),
            )
            self._conn.commit()
            return LockRecord(slug, holder_id, fencing_token, now, expires)

    async def release(
        self, slug: str, holder_id: str, fencing_token: int
    ) -> bool:
        async with self._mu:
            row = self._conn.execute(
                "SELECT holder_id, fencing_token FROM neighborhood_locks"
                " WHERE slug = ?",
                (slug,),
            ).fetchone()
            if row is None:
                return False
            if row[0] != holder_id or int(row[1]) != fencing_token:
                return False
            self._conn.execute(
                "DELETE FROM neighborhood_locks WHERE slug = ?", (slug,)
            )
            self._conn.commit()
            return True

    async def peek(self, slug: str) -> Optional[LockRecord]:
        row = self._conn.execute(
            "SELECT slug, holder_id, fencing_token, acquired_at, expires_at"
            " FROM neighborhood_locks WHERE slug = ?",
            (slug,),
        ).fetchone()
        if row is None:
            return None
        return LockRecord(
            slug=row[0],
            holder_id=row[1],
            fencing_token=int(row[2]),
            acquired_at=_parse_iso(row[3]),
            expires_at=_parse_iso(row[4]),
        )

    async def close(self) -> None:
        # Connection is owned by the backend; nothing to release here.
        return None

    def _bump_fence(self, slug: str) -> int:
        row = self._conn.execute(
            "SELECT current_token FROM neighborhood_fence_tokens"
            " WHERE slug = ?",
            (slug,),
        ).fetchone()
        new_token = int(row[0]) + 1 if row else 1
        self._conn.execute(
            "INSERT INTO neighborhood_fence_tokens(slug, current_token)"
            " VALUES (?, ?)"
            " ON CONFLICT(slug) DO UPDATE SET current_token = excluded.current_token",
            (slug, new_token),
        )
        return new_token


# ---------------------------------------------------------------------------
# Markdown (sidecar-file) registry
# ---------------------------------------------------------------------------


class MarkdownLockRegistry:
    """Filesystem-only registry. One sidecar file per lock.

    Layout::

        <vault>/.memu/locks/<slug>.json          lease payload
        <vault>/.memu/locks/_fence/<slug>        monotonic fencing counter

    Atomicity hinges on two primitives:

    - ``os.O_CREAT | os.O_EXCL`` for lease creation so two concurrent acquire
      attempts on the same filesystem can't both succeed.
    - Write-to-tmp-then-``rename`` for fence/lease mutations so readers never
      see torn payloads.
    """

    def __init__(self, root: os.PathLike[str] | str):
        self.root = Path(root)
        self._locks_dir = self.root / ".memu" / "locks"
        self._fence_dir = self._locks_dir / "_fence"
        self._mu = asyncio.Lock()

    async def init(self) -> None:
        self._locks_dir.mkdir(parents=True, exist_ok=True)
        self._fence_dir.mkdir(parents=True, exist_ok=True)

    async def try_acquire(
        self, slug: str, holder_id: str, ttl_s: float
    ) -> LockRecord:
        async with self._mu:
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=ttl_s)
            lock_path = self._lock_path(slug)

            # Clear an expired lease first so the O_EXCL create below can
            # succeed on reclaim.
            existing = self._read_lock_file(lock_path)
            if existing is not None:
                if _parse_iso(existing["expires_at"]) > now:
                    raise NeighborhoodConflict(
                        f"slug {slug!r} held by {existing['holder_id']!r}"
                        f" until {existing['expires_at']}"
                    )
                lock_path.unlink(missing_ok=True)

            new_token = self._bump_fence(slug)
            payload = {
                "slug": slug,
                "holder_id": holder_id,
                "fencing_token": new_token,
                "acquired_at": now.isoformat(),
                "expires_at": expires.isoformat(),
            }
            try:
                fd = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError as e:
                raise NeighborhoodConflict(
                    f"slug {slug!r}: lost race to acquire"
                ) from e
            try:
                os.write(fd, json.dumps(payload).encode("utf-8"))
            finally:
                os.close(fd)
            return LockRecord(slug, holder_id, new_token, now, expires)

    async def renew(
        self, slug: str, holder_id: str, fencing_token: int, ttl_s: float
    ) -> LockRecord:
        async with self._mu:
            self._assert_fence_matches(slug, fencing_token)
            lock_path = self._lock_path(slug)
            payload = self._read_lock_file(lock_path)
            if payload is None:
                raise NeighborhoodConflict(
                    f"slug {slug!r}: lock no longer present"
                )
            if (
                payload.get("holder_id") != holder_id
                or int(payload.get("fencing_token", -1)) != fencing_token
            ):
                raise NeighborhoodConflict(
                    f"slug {slug!r}: owner changed"
                )
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=ttl_s)
            payload["expires_at"] = expires.isoformat()
            self._atomic_write(lock_path, json.dumps(payload))
            return LockRecord(slug, holder_id, fencing_token, now, expires)

    async def release(
        self, slug: str, holder_id: str, fencing_token: int
    ) -> bool:
        async with self._mu:
            lock_path = self._lock_path(slug)
            payload = self._read_lock_file(lock_path)
            if payload is None:
                return False
            if (
                payload.get("holder_id") != holder_id
                or int(payload.get("fencing_token", -1)) != fencing_token
            ):
                return False
            # Honor fencing_token: the on-disk counter must still match the
            # caller's token. If a reclaim already bumped it, the lease they
            # hold is stale and release is a no-op.
            if self._read_fence(slug) != fencing_token:
                return False
            lock_path.unlink(missing_ok=True)
            return True

    async def peek(self, slug: str) -> Optional[LockRecord]:
        lock_path = self._lock_path(slug)
        payload = self._read_lock_file(lock_path)
        if payload is None:
            return None
        return LockRecord(
            slug=str(payload["slug"]),
            holder_id=str(payload["holder_id"]),
            fencing_token=int(payload["fencing_token"]),
            acquired_at=_parse_iso(payload["acquired_at"]),
            expires_at=_parse_iso(payload["expires_at"]),
        )

    async def close(self) -> None:
        return None

    # --- internal helpers -------------------------------------------------

    def _safe_name(self, slug: str) -> str:
        # Slugs may include '/' (e.g., "paper/attention"). Collapse to a flat
        # filename so the locks dir stays one level deep.
        return slug.replace("/", "__")

    def _lock_path(self, slug: str) -> Path:
        return self._locks_dir / f"{self._safe_name(slug)}.json"

    def _fence_path(self, slug: str) -> Path:
        return self._fence_dir / self._safe_name(slug)

    def _read_lock_file(self, path: Path) -> Optional[dict]:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _read_fence(self, slug: str) -> int:
        try:
            return int(self._fence_path(slug).read_text().strip() or "0")
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            return 0

    def _bump_fence(self, slug: str) -> int:
        new_token = self._read_fence(slug) + 1
        self._atomic_write(self._fence_path(slug), str(new_token))
        return new_token

    def _assert_fence_matches(self, slug: str, expected: int) -> None:
        current = self._read_fence(slug)
        if current != expected:
            raise NeighborhoodConflict(
                f"slug {slug!r}: fencing token mismatch"
                f" (held {expected}, current {current})"
            )

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class NeighborhoodLock:
    """Async context manager that acquires a slug (plus optional 1-hop
    neighbors) and keeps the lease alive via an async renewer task.

    Usage::

        registry = backend.get_lock_registry()
        await registry.init()
        async with NeighborhoodLock(registry, "foo-design", neighbors=["bar"]):
            # exclusive on foo-design + bar
            ...

    Multiple slugs are acquired in a sorted order; if any one fails, every
    lock already grabbed is released before the :class:`NeighborhoodConflict`
    propagates, so callers never leak partial leases.
    """

    def __init__(
        self,
        registry: LockRegistry,
        slug: str,
        *,
        neighbors: Iterable[str] = (),
        holder_id: Optional[str] = None,
        ttl_s: float = DEFAULT_TTL_S,
        renew_interval_s: float = DEFAULT_RENEW_INTERVAL_S,
    ):
        self.registry = registry
        self.slug = slug
        self.neighbors = tuple(neighbors)
        self.holder_id = holder_id or uuid.uuid4().hex
        self.ttl_s = float(ttl_s)
        self.renew_interval_s = float(renew_interval_s)
        if self.renew_interval_s <= 0:
            raise ValueError("renew_interval_s must be positive")
        if self.renew_interval_s >= self.ttl_s:
            raise ValueError("renew_interval_s must be < ttl_s")
        self.records: list[LockRecord] = []
        self._renewer: Optional[asyncio.Task] = None

    @property
    def record(self) -> LockRecord:
        if not self.records:
            raise RuntimeError("NeighborhoodLock is not currently held")
        return self.records[0]

    @property
    def fencing_token(self) -> int:
        return self.record.fencing_token

    async def __aenter__(self) -> "NeighborhoodLock":
        targets = [self.slug] + sorted(
            s for s in self.neighbors if s != self.slug
        )
        acquired: list[LockRecord] = []
        try:
            for slug in targets:
                rec = await self.registry.try_acquire(
                    slug, self.holder_id, self.ttl_s
                )
                acquired.append(rec)
        except BaseException:
            for rec in acquired:
                try:
                    await self.registry.release(
                        rec.slug, rec.holder_id, rec.fencing_token
                    )
                except Exception:
                    logger.exception(
                        "rollback release failed for %s", rec.slug
                    )
            raise
        self.records = acquired
        self._renewer = asyncio.create_task(
            self._renew_loop(), name=f"nlock-renew:{self.slug}"
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
            try:
                await self._renewer
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("renewer task exited with error")
            self._renewer = None
        for rec in self.records:
            try:
                await self.registry.release(
                    rec.slug, rec.holder_id, rec.fencing_token
                )
            except Exception:
                logger.exception("release failed for %s", rec.slug)
        self.records = []

    async def _renew_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.renew_interval_s)
                renewed: list[LockRecord] = []
                for rec in self.records:
                    renewed.append(
                        await self.registry.renew(
                            rec.slug,
                            rec.holder_id,
                            rec.fencing_token,
                            self.ttl_s,
                        )
                    )
                self.records = renewed
        except asyncio.CancelledError:
            raise
        except NeighborhoodConflict:
            # The lock was stolen; surface by letting the task die. Callers
            # inside the ``async with`` body will notice on their next
            # interaction with the registry (or when the body exits).
            logger.warning(
                "NeighborhoodLock renew lost ownership for %s", self.slug
            )
        except Exception:
            logger.exception("renew loop crashed for %s", self.slug)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    """Parse ISO timestamps produced by datetime.isoformat(), tolerating
    trailing ``Z`` notation."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


__all__ = [
    "DEFAULT_TTL_S",
    "DEFAULT_RENEW_INTERVAL_S",
    "LockRecord",
    "LockRegistry",
    "MarkdownLockRegistry",
    "NeighborhoodConflict",
    "NeighborhoodLock",
    "SqliteLockRegistry",
]
