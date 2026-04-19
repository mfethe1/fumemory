from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from memu.ingest import ingest_codebase
from memu.storage import get_backend


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_pkg"


def _copy_fixture(tmp_path: Path) -> Path:
    dst = tmp_path / "tiny_pkg"
    shutil.copytree(FIXTURE, dst)
    return dst


def _backend_for(tmp_path: Path):
    return get_backend(f"sqlite:///{tmp_path / 'index.db'}")


@pytest.mark.asyncio
async def test_ingests_all_top_level_symbols(tmp_path):
    src = _copy_fixture(tmp_path)
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        result = await ingest_codebase(backend, src, repo_url="https://example.com/repo")
        added = set(result.added)
        expected = {
            "code/tiny_pkg",                            # __init__ (re-exports only)
            "code/tiny_pkg/core",
            "code/tiny_pkg/core/Greeter",
            "code/tiny_pkg/core/Greeter.__init__",
            "code/tiny_pkg/core/Greeter.greet",
            "code/tiny_pkg/core/Greeter._render",
            "code/tiny_pkg/core/make_greeter",
            "code/tiny_pkg/utils",
            "code/tiny_pkg/utils/format_hello",
            "code/tiny_pkg/utils/shout",
            "code/tiny_pkg/_private",
            "code/tiny_pkg/_private/hidden_helper",
        }
        missing = expected - added
        assert not missing, f"missing slugs: {missing}"

        greeter = await backend.get_node("code/tiny_pkg/core/Greeter")
        assert greeter is not None
        assert "class Greeter" in (greeter.source or {}).get("signature", "")
        assert greeter.source["module"] == "tiny_pkg.core"
        assert greeter.source["path"] == "tiny_pkg/core.py"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_methods_link_back_to_their_class(tmp_path):
    src = _copy_fixture(tmp_path)
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        await ingest_codebase(backend, src)
        greet = await backend.get_node("code/tiny_pkg/core/Greeter.greet")
        assert greet is not None
        dst_slugs = {l.dst_slug for l in greet.links}
        assert "code/tiny_pkg/core/Greeter" in dst_slugs
        # self._render() is linked.
        assert "code/tiny_pkg/core/Greeter._render" in dst_slugs
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_cross_module_imports_become_wikilinks(tmp_path):
    src = _copy_fixture(tmp_path)
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        await ingest_codebase(backend, src)
        render = await backend.get_node("code/tiny_pkg/core/Greeter._render")
        assert render is not None
        dst = {l.dst_slug for l in render.links}
        # u.format_hello resolves to utils.format_hello via the aliased import.
        assert "code/tiny_pkg/utils/format_hello" in dst

        factory = await backend.get_node("code/tiny_pkg/core/make_greeter")
        assert factory is not None
        dst2 = {l.dst_slug for l in factory.links}
        # hidden_helper comes from ``from ._private import hidden_helper``.
        assert "code/tiny_pkg/_private/hidden_helper" in dst2
        # Greeter() call links to the class in the same module.
        assert "code/tiny_pkg/core/Greeter" in dst2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_reingest_is_idempotent(tmp_path):
    src = _copy_fixture(tmp_path)
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        first = await ingest_codebase(backend, src)
        assert len(first.added) > 0
        second = await ingest_codebase(backend, src)
        assert second.added == []
        assert second.updated == []
        assert len(second.unchanged) == len(first.added)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_changing_a_file_updates_only_that_file(tmp_path):
    src = _copy_fixture(tmp_path)
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        await ingest_codebase(backend, src)
        utils = src / "tiny_pkg" / "utils.py"
        utils.write_text(
            utils.read_text(encoding="utf-8") + "\n\ndef extra():\n    return 2\n",
            encoding="utf-8",
        )
        result = await ingest_codebase(backend, src)
        all_changed = set(result.added) | set(result.updated)
        # All touched nodes are in utils; no other module should move.
        assert all(slug.startswith("code/tiny_pkg/utils") for slug in all_changed)
        assert "code/tiny_pkg/utils/extra" in result.added
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_gitignore_is_respected_during_ingest(tmp_path):
    src = _copy_fixture(tmp_path)
    (src / "ignored_file.py").write_text("x = 1\n", encoding="utf-8")
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        await ingest_codebase(backend, src)
        node = await backend.get_node("code/ignored_file")
        assert node is None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_force_rewrites_everything(tmp_path):
    src = _copy_fixture(tmp_path)
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        first = await ingest_codebase(backend, src)
        assert first.added
        second = await ingest_codebase(backend, src, force=True)
        assert second.updated  # everything was rewritten.
        assert second.added == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_exclude_skips_glob(tmp_path):
    src = _copy_fixture(tmp_path)
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        await ingest_codebase(backend, src, excludes=["**/_private.py"])
        assert await backend.get_node("code/tiny_pkg/_private") is None
        assert await backend.get_node("code/tiny_pkg/core/Greeter") is not None
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_root_that_is_itself_a_package_uses_its_own_name(tmp_path):
    # When `memu ingest code path/to/pkg` points at a directory that
    # already contains an __init__.py, the resulting module names must
    # carry the root's own name — otherwise the top-level becomes
    # ``code/`` which is both ugly and collision-prone.
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""My top-level package."""\n', encoding="utf-8")
    (pkg / "thing.py").write_text(
        "def it():\n    return 42\n", encoding="utf-8"
    )
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        result = await ingest_codebase(backend, pkg)
        assert "code/mypkg" in result.added
        assert "code/mypkg/thing" in result.added
        assert "code/mypkg/thing/it" in result.added
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_commit_sha_recorded_when_available(tmp_path):
    src = _copy_fixture(tmp_path)
    # Create a fake .git directory with a resolvable HEAD.
    git_dir = tmp_path / "tiny_pkg" / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(
        "abc123def4567890abc123def4567890abc123de\n", encoding="utf-8"
    )
    backend = _backend_for(tmp_path)
    await backend.init()
    try:
        result = await ingest_codebase(backend, src)
        assert result.commit_sha == "abc123def4567890abc123def4567890abc123de"
        node = await backend.get_node("code/tiny_pkg/core/Greeter")
        assert node is not None
        assert (node.source or {}).get("commit") == result.commit_sha
    finally:
        await backend.close()
