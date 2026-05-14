"""Keep a memU vault in sync with its index backend.

Filesystem is source of truth. This module walks the vault (one-shot)
or watches it for changes (long-running) and pushes each ``.md`` file
through the active :class:`StorageBackend` so queries, links, and
wikilink backlinks stay fresh as Obsidian writes to the vault.

Two layers
----------

* :class:`VaultSyncer` — the testable core. Given a path, it parses
  frontmatter + body, resolves the node id (from frontmatter or a
  deterministic derivation so re-indexes stay stable), extracts
  outbound ``[[wikilinks]]``, and calls ``backend.put_node``. Each call
  returns a :class:`SyncAction` so callers can summarize what happened.

* :class:`SyncWatcher` — the I/O glue. Uses the ``watchdog`` package
  when available, falls back to a simple ``mtime`` poller so the
  daemon runs on every platform without requiring a C extension. The
  watcher debounces rapid bursts per path (Obsidian autosave triggers
  many events for a single edit).

The daemon does *not* write to the filesystem. That keeps it loop-safe
(no "react to my own write" feedback) and lets users edit freely in
Obsidian, VS Code, or any other tool. Bidirectional DB→fs sync is a
follow-up that belongs in a separate module.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal, Optional

from ..storage.base import LinkRecord, StorageBackend, WikiNode
from .frontmatter import parse_frontmatter
from .vault import VaultLayout
from .wikilink import parse_wikilinks


log = logging.getLogger(__name__)

SyncOutcome = Literal["added", "updated", "unchanged", "skipped", "deleted", "error"]


@dataclass(frozen=True)
class SyncAction:
    path: Path
    slug: Optional[str]
    outcome: SyncOutcome
    reason: str = ""


@dataclass
class SyncReport:
    actions: list[SyncAction] = field(default_factory=list)
    scanned: int = 0
    elapsed_ms: float = 0.0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a.outcome] = out.get(a.outcome, 0) + 1
        return out

    def summary(self) -> str:
        c = self.counts()
        fields = " ".join(f"{k}={v}" for k, v in sorted(c.items()))
        return f"{fields} scanned={self.scanned} elapsed={self.elapsed_ms:.0f}ms"


# ---------------------------------------------------------------------------
# Core syncer
# ---------------------------------------------------------------------------


class VaultSyncer:
    """Reindexes files in a memU vault into a storage backend.

    The syncer is stateless across calls; it relies on the backend to
    record the last-seen content hash (stored in ``metadata.content_hash``)
    so incremental updates are cheap.
    """

    def __init__(self, backend: StorageBackend, vault_root: Path):
        self.backend = backend
        self.vault_root = vault_root.resolve()
        self.layout = VaultLayout(self.vault_root)

    # ---- public API ------------------------------------------------------

    async def reindex_file(self, path: Path) -> SyncAction:
        path = path.resolve()
        if not self._is_vault_markdown(path):
            return SyncAction(path=path, slug=None, outcome="skipped", reason="not-vault-md")
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return SyncAction(path=path, slug=None, outcome="error", reason=str(exc))
        return await self._sync_text(path, raw)

    async def reindex_all(self) -> SyncReport:
        started = time.perf_counter()
        report = SyncReport()
        for path in self._iter_vault_markdown():
            action = await self.reindex_file(path)
            report.actions.append(action)
            report.scanned += 1
        report.elapsed_ms = (time.perf_counter() - started) * 1000
        return report

    async def handle_delete(self, path: Path) -> SyncAction:
        """A vault file disappeared. By default we do NOT delete the
        backend node — users curate. Override in subclasses for
        destructive sync. This returns a ``deleted``-outcome record for
        reporting only.
        """
        return SyncAction(path=path, slug=None, outcome="deleted", reason="file-removed")

    # ---- internals -------------------------------------------------------

    def _is_vault_markdown(self, path: Path) -> bool:
        if path.suffix.lower() != ".md":
            return False
        try:
            path.resolve().relative_to(self.vault_root)
        except ValueError:
            return False
        parts = path.resolve().relative_to(self.vault_root).parts
        # Skip the derived/cache zones.
        if ".memu" in parts:
            return False
        if parts and parts[0] == "_index":
            # _index/master.md is special (LLM-maintained TOC); we still
            # index it, but derivatives like backlinks.json are filtered
            # by suffix above.
            return parts[-1].endswith(".md")
        return True

    def _iter_vault_markdown(self) -> Iterable[Path]:
        for path in self.vault_root.rglob("*.md"):
            if self._is_vault_markdown(path):
                yield path

    async def _sync_text(self, path: Path, raw: str) -> SyncAction:
        fm, body = parse_frontmatter(raw)
        slug = self._slug_for(path, fm)
        if not slug:
            return SyncAction(path=path, slug=None, outcome="skipped", reason="no-slug")
        content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        existing = await self.backend.get_node(slug)
        if existing is not None:
            existing_hash = (existing.metadata or {}).get("content_hash")
            if existing_hash == content_hash:
                return SyncAction(path=path, slug=slug, outcome="unchanged")

        node_id = (existing.id if existing else fm.get("id")) or _derive_stable_id(path, self.vault_root)
        kind = fm.get("kind") or _infer_kind_from_path(path, self.vault_root)
        links = [
            LinkRecord(src_slug=slug, dst_slug=link.slug, type="related")
            for link in parse_wikilinks(body)
        ]
        now = datetime.now(timezone.utc)
        node = WikiNode(
            id=str(node_id),
            slug=slug,
            kind=kind,
            title=fm.get("title") or path.stem,
            body=body,
            tags=list(fm.get("tags") or []),
            links=links,
            metadata={**(fm.get("metadata") or {}), "content_hash": content_hash},
            agent_id=fm.get("agent_id", "vault-sync"),
            memory_type=fm.get("memory_type", "observation"),
            salience=_as_float(fm.get("salience"), 0.5),
            confidence=_as_float(fm.get("confidence"), 1.0),
            source=fm.get("source"),
            created_at=_parse_datetime(fm.get("created")) or (existing.created_at if existing else now),
            updated_at=now,
        )
        await self.backend.put_node(node)
        return SyncAction(
            path=path,
            slug=slug,
            outcome="updated" if existing else "added",
        )

    def _slug_for(self, path: Path, fm: dict[str, Any]) -> Optional[str]:
        fm_slug = fm.get("slug")
        if isinstance(fm_slug, str) and fm_slug:
            return fm_slug
        try:
            rel = path.resolve().relative_to(self.vault_root)
        except ValueError:
            return None
        parts = list(rel.parts)
        if not parts:
            return None
        last = parts[-1]
        if last.endswith(".md"):
            parts[-1] = last[:-3]
        # Normalize the tier prefix to match the singular namespace the
        # slug registry dispatches on:
        #   notes/foo       -> foo              (default tier, stripped)
        #   papers/atn      -> paper/atn        (plural -> singular)
        #   tasks/t1        -> task/t1
        #   code/mod/bar    -> code/mod/bar     (already singular)
        _TIER_SLUG_PREFIX = {
            "notes": None,
            "papers": "paper",
            "tasks": "task",
            "code": "code",
        }
        if parts and parts[0] in _TIER_SLUG_PREFIX:
            replacement = _TIER_SLUG_PREFIX[parts[0]]
            parts = parts[1:] if replacement is None else [replacement, *parts[1:]]
        return "/".join(parts) or None


def _infer_kind_from_path(path: Path, root: Path) -> str:
    try:
        parts = path.resolve().relative_to(root).parts
    except ValueError:
        return "note"
    if not parts:
        return "note"
    first = parts[0]
    if first == "code":
        return "code"
    if first == "papers":
        return "paper"
    if first == "tasks":
        return "task"
    return "note"


def _derive_stable_id(path: Path, root: Path) -> str:
    """Stable UUIDv5 from (vault_root, rel_path).

    When a file lacks ``id`` in its frontmatter we need an id that
    won't drift across re-ingests. UUIDv5 gives us deterministic
    derivation without writing back to the file, which keeps the
    daemon loop-safe.
    """
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        rel = path.as_posix()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memu-vault://{root.as_posix()}::{rel}"))


def _parse_datetime(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_float(val: Any, default: float) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Debouncer
# ---------------------------------------------------------------------------


class _Debouncer:
    """Coalesces rapid-fire events per key within a fixed window.

    Used to collapse Obsidian's multi-write bursts (editor autosave +
    plugin writes + .obsidian meta writes) into a single reindex call.
    """

    def __init__(self, window_ms: int = 500):
        self.window_ms = window_ms
        self._deadlines: dict[str, float] = {}

    def should_fire(self, key: str) -> bool:
        """Returns True if the key has not been seen within ``window_ms``.

        The caller fires the work and then calls :meth:`mark` so
        subsequent events inside the window are dropped.
        """
        now = time.monotonic() * 1000
        deadline = self._deadlines.get(key, 0)
        return now >= deadline

    def mark(self, key: str) -> None:
        self._deadlines[key] = time.monotonic() * 1000 + self.window_ms


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


PathCallback = Callable[[Path, Literal["modified", "created", "deleted"]], Awaitable[None]]


class PollingWatcher:
    """Dependency-free file watcher.

    Checks every ``poll_seconds`` for new / modified / deleted files.
    Usable everywhere Python runs — no C extension, no inotify/kqueue
    bindings. For vaults larger than a few thousand files, users should
    ``pip install watchdog`` and use :func:`SyncWatcher` (which picks
    the faster backend automatically).
    """

    def __init__(
        self,
        root: Path,
        callback: PathCallback,
        *,
        poll_seconds: float = 1.0,
        initial_snapshot: Optional[dict[Path, float]] = None,
    ):
        self.root = root.resolve()
        self.callback = callback
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()
        self._initial_snapshot = initial_snapshot

    async def run(self) -> None:
        # If the caller pre-captured a snapshot (e.g. SyncDaemon.start
        # takes one before yielding control, closing the race between
        # start() returning and the first poll), use that. Otherwise
        # snapshot now.
        snapshot = (
            self._initial_snapshot
            if self._initial_snapshot is not None
            else self._snapshot()
        )
        self._initial_snapshot = None
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
            new_snapshot = self._snapshot()
            await self._diff_and_dispatch(snapshot, new_snapshot)
            snapshot = new_snapshot

    def stop(self) -> None:
        self._stop.set()

    # ---- internals
    def _snapshot(self) -> dict[Path, float]:
        snap: dict[Path, float] = {}
        for p in self.root.rglob("*.md"):
            try:
                snap[p.resolve()] = p.stat().st_mtime
            except OSError:
                continue
        return snap

    async def _diff_and_dispatch(
        self,
        old: dict[Path, float],
        new: dict[Path, float],
    ) -> None:
        for path, mtime in new.items():
            prev = old.get(path)
            if prev is None:
                await self._safe_call(path, "created")
            elif mtime > prev:
                await self._safe_call(path, "modified")
        for path in old.keys() - new.keys():
            await self._safe_call(path, "deleted")

    async def _safe_call(self, path: Path, event: str) -> None:
        try:
            await self.callback(path, event)  # type: ignore[arg-type]
        except Exception:
            log.exception("Sync callback failed for %s (%s)", path, event)


# ---------------------------------------------------------------------------
# High-level daemon
# ---------------------------------------------------------------------------


class SyncDaemon:
    """Glue: PollingWatcher + VaultSyncer + debouncer.

    Constructing the daemon does not start it; call :meth:`start` to
    kick off the watcher task and :meth:`stop` to drain it cleanly.
    """

    def __init__(
        self,
        backend: StorageBackend,
        vault_root: Path,
        *,
        poll_seconds: float = 1.0,
        debounce_ms: int = 500,
    ) -> None:
        self.syncer = VaultSyncer(backend, vault_root)
        self._debouncer = _Debouncer(window_ms=debounce_ms)
        self._vault_root = vault_root.resolve()
        self._poll_seconds = poll_seconds
        self._watcher: Optional[PollingWatcher] = None
        self._task: Optional[asyncio.Task[None]] = None
        self.last_report: Optional[SyncReport] = None

    async def _handle(
        self,
        path: Path,
        event: Literal["modified", "created", "deleted"],
    ) -> None:
        key = str(path)
        if not self._debouncer.should_fire(key):
            return
        self._debouncer.mark(key)
        if event == "deleted":
            await self.syncer.handle_delete(path)
            return
        await self.syncer.reindex_file(path)

    async def start(self) -> None:
        if self._task is not None:
            return
        # Run a one-shot full reindex first so the backend is warm.
        self.last_report = await self.syncer.reindex_all()
        # Capture the snapshot *now* — before returning control — so
        # that any file a caller writes between ``start()`` returning
        # and the first poll cycle is recognized as "new" rather than
        # silently folded into the initial state.
        watcher = PollingWatcher(
            self._vault_root,
            self._handle,
            poll_seconds=self._poll_seconds,
            initial_snapshot=self._snapshot_now(),
        )
        self._watcher = watcher
        self._task = asyncio.create_task(watcher.run(), name="memu-sync-watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        assert self._watcher is not None
        self._watcher.stop()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._watcher = None

    def _snapshot_now(self) -> dict[Path, float]:
        snap: dict[Path, float] = {}
        for p in self._vault_root.rglob("*.md"):
            try:
                snap[p.resolve()] = p.stat().st_mtime
            except OSError:
                continue
        return snap


__all__ = [
    "VaultSyncer",
    "SyncDaemon",
    "SyncAction",
    "SyncReport",
    "PollingWatcher",
]
