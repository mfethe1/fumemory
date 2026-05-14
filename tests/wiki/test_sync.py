from __future__ import annotations

import asyncio
import textwrap

import pytest

from memu.storage import get_backend
from memu.wiki.sync import SyncDaemon, VaultSyncer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _vault(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "code").mkdir()
    (tmp_path / "tasks").mkdir()
    (tmp_path / "papers").mkdir()
    return tmp_path


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Strip the leading newline left by triple-quoted literals so
    # frontmatter starts on line 1 (Obsidian's rule).
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


# ---------------------------------------------------------------------------
# VaultSyncer: fs -> backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reindex_file_adds_node_with_path_derived_slug(tmp_path):
    root = _vault(tmp_path)
    _write(root / "notes" / "hello.md", "# Hello\nBody with [[world]].\n")
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        syncer = VaultSyncer(backend, root)
        action = await syncer.reindex_file(root / "notes" / "hello.md")
        assert action.outcome == "added"
        assert action.slug == "hello"
        node = await backend.get_node("hello")
        assert node is not None
        assert any(l.dst_slug == "world" for l in node.links)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_frontmatter_slug_overrides_path(tmp_path):
    root = _vault(tmp_path)
    _write(
        root / "notes" / "renamed.md",
        """
        ---
        slug: canonical-slug
        title: Canonical
        ---
        body
        """,
    )
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        syncer = VaultSyncer(backend, root)
        action = await syncer.reindex_file(root / "notes" / "renamed.md")
        assert action.slug == "canonical-slug"
        assert await backend.get_node("canonical-slug") is not None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_kind_inferred_from_tier_directory(tmp_path):
    root = _vault(tmp_path)
    _write(root / "code" / "foo" / "bar.md", "# bar\n")
    _write(root / "papers" / "atn.md", "# atn\n")
    _write(root / "tasks" / "t1.md", "# t1\n")
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        syncer = VaultSyncer(backend, root)
        for rel in ("code/foo/bar.md", "papers/atn.md", "tasks/t1.md"):
            await syncer.reindex_file(root / rel)
        code_node = await backend.get_node("code/foo/bar")
        paper_node = await backend.get_node("paper/atn")
        task_node = await backend.get_node("task/t1")
        assert code_node and code_node.kind == "code"
        assert paper_node and paper_node.kind == "paper"
        assert task_node and task_node.kind == "task"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_content_hash_makes_reindex_unchanged(tmp_path):
    root = _vault(tmp_path)
    _write(root / "notes" / "hello.md", "# Hello\n")
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        syncer = VaultSyncer(backend, root)
        first = await syncer.reindex_file(root / "notes" / "hello.md")
        assert first.outcome == "added"
        second = await syncer.reindex_file(root / "notes" / "hello.md")
        assert second.outcome == "unchanged"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_edit_triggers_update_preserving_node_id(tmp_path):
    root = _vault(tmp_path)
    path = root / "notes" / "hello.md"
    _write(path, "# Hello v1\n")
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        syncer = VaultSyncer(backend, root)
        await syncer.reindex_file(path)
        before = await backend.get_node("hello")
        assert before is not None
        original_id = before.id

        path.write_text("# Hello v2\n\nnew body\n", encoding="utf-8")
        action = await syncer.reindex_file(path)
        assert action.outcome == "updated"
        after = await backend.get_node("hello")
        assert after is not None
        assert after.id == original_id
        assert "v2" in after.body
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_reindex_all_reports_counts(tmp_path):
    root = _vault(tmp_path)
    _write(root / "notes" / "a.md", "# a\n")
    _write(root / "notes" / "b.md", "# b\n")
    _write(root / "code" / "foo" / "bar.md", "# bar\n")
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        syncer = VaultSyncer(backend, root)
        report = await syncer.reindex_all()
        assert report.scanned == 3
        assert report.counts().get("added", 0) == 3
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_skips_files_outside_vault_and_in_memu_cache(tmp_path):
    root = _vault(tmp_path)
    _write(root / ".memu" / "cache.md", "# cache\n")
    outside = tmp_path.parent / "other.md"
    _write(outside, "# outside\n")
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        syncer = VaultSyncer(backend, root)
        report = await syncer.reindex_all()
        assert all("cache" not in (a.slug or "") for a in report.actions)
        action = await syncer.reindex_file(outside)
        assert action.outcome == "skipped"
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# SyncDaemon: watcher loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_picks_up_new_files(tmp_path):
    root = _vault(tmp_path)
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        daemon = SyncDaemon(backend, root, poll_seconds=0.05, debounce_ms=0)
        await daemon.start()
        try:
            _write(root / "notes" / "fresh.md", "# Fresh\n")
            # Poll until the node shows up, with a generous timeout.
            deadline = asyncio.get_event_loop().time() + 5.0
            node = None
            while asyncio.get_event_loop().time() < deadline:
                node = await backend.get_node("fresh")
                if node is not None:
                    break
                await asyncio.sleep(0.1)
            assert node is not None, "daemon did not index new file within 5s"
        finally:
            await daemon.stop()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_daemon_picks_up_modifications(tmp_path):
    root = _vault(tmp_path)
    path = root / "notes" / "evolving.md"
    _write(path, "# v1\n")
    backend = get_backend(f"sqlite:///{tmp_path / 'i.db'}")
    await backend.init()
    try:
        daemon = SyncDaemon(backend, root, poll_seconds=0.05, debounce_ms=0)
        await daemon.start()
        try:
            first = await backend.get_node("evolving")
            assert first is not None

            # Bump mtime explicitly: tmp_path can coarsen mtimes enough
            # that a quick overwrite doesn't register as modified.
            import os, time
            new_mtime = path.stat().st_mtime + 5
            path.write_text("# v2\n\nchanged body\n", encoding="utf-8")
            os.utime(path, (new_mtime, new_mtime))

            deadline = asyncio.get_event_loop().time() + 5.0
            updated = first
            while asyncio.get_event_loop().time() < deadline:
                updated = await backend.get_node("evolving")
                if updated is not None and "v2" in (updated.body or ""):
                    break
                await asyncio.sleep(0.1)
            assert "v2" in (updated.body or ""), "daemon did not pick up modification"
        finally:
            await daemon.stop()
    finally:
        await backend.close()
