from __future__ import annotations


from memu.ingest.filters import build_walker


def _touch(path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_default_directories_are_skipped(tmp_path):
    _touch(tmp_path / "a.py", "")
    _touch(tmp_path / "__pycache__" / "cached.py", "")
    _touch(tmp_path / "node_modules" / "x.py", "")
    _touch(tmp_path / ".git" / "HEAD", "ref: refs/heads/main\n")

    walker = build_walker(tmp_path)
    files = {p.name for p in walker.iter_files()}
    assert files == {"a.py"}


def test_gitignore_is_honored(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    _touch(tmp_path / "kept.py", "")
    _touch(tmp_path / "ignored.py", "")

    walker = build_walker(tmp_path)
    files = {p.name for p in walker.iter_files()}
    assert "kept.py" in files
    assert "ignored.py" not in files


def test_exclude_glob_skips_matching_files(tmp_path):
    _touch(tmp_path / "pkg" / "mod.py", "")
    _touch(tmp_path / "tests" / "test_mod.py", "")

    walker = build_walker(tmp_path, excludes=["tests/**"])
    rels = {p.relative_to(tmp_path).as_posix() for p in walker.iter_files()}
    assert rels == {"pkg/mod.py"}


def test_include_restricts_to_glob(tmp_path):
    _touch(tmp_path / "pkg" / "mod.py", "")
    _touch(tmp_path / "docs" / "readme.md", "")

    walker = build_walker(tmp_path, includes=["pkg/**"])
    rels = {p.relative_to(tmp_path).as_posix() for p in walker.iter_files()}
    assert rels == {"pkg/mod.py"}


def test_nested_gitignore_overrides_parent(tmp_path):
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    _touch(tmp_path / "sub" / "ignored.py", "")
    (tmp_path / "sub" / ".gitignore").write_text("!ignored.py\n", encoding="utf-8")

    walker = build_walker(tmp_path)
    rels = {p.relative_to(tmp_path).as_posix() for p in walker.iter_files()}
    assert "sub/ignored.py" in rels
