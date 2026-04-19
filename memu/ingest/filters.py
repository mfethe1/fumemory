"""Filesystem walk + ignore logic for codebase ingestion.

``build_walker`` returns an iterator of ``Path`` for source files under a
root, respecting:

* A built-in blocklist of directories (``.git``, ``__pycache__``,
  ``node_modules``, …).
* User-supplied glob excludes (``tests/**``, ``**/_vendor/**``).
* The repository's own ``.gitignore`` files, when present and
  ``respect_gitignore=True``.

We deliberately do *not* pull in the ``pathspec`` package — the subset of
gitignore syntax we support (literal paths, ``/``-anchored, ``*`` /
``**`` / ``?`` wildcards, ``!`` negation, trailing-slash directory
markers) is enough for typical Python projects and keeps memU's runtime
dependency footprint minimal.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional


DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "target",
    "_index",
    ".memu",
})


def build_walker(
    root: Path,
    *,
    includes: Optional[list[str]] = None,
    excludes: Optional[list[str]] = None,
    respect_gitignore: bool = True,
    follow_symlinks: bool = False,
) -> "Walker":
    root = root.resolve()
    return Walker(
        root=root,
        includes=list(includes or []),
        excludes=list(excludes or []),
        respect_gitignore=respect_gitignore,
        follow_symlinks=follow_symlinks,
    )


class Walker:
    """Iterates paths under ``root`` honoring ignore rules.

    Designed to be cheap to construct and reusable: each ``iter_files()``
    call re-walks the tree, so adding a file after construction does not
    require a new walker.
    """

    def __init__(
        self,
        *,
        root: Path,
        includes: list[str],
        excludes: list[str],
        respect_gitignore: bool,
        follow_symlinks: bool,
    ) -> None:
        self.root = root
        self.includes = includes
        self.excludes = excludes
        self.follow_symlinks = follow_symlinks
        self._gitignores: dict[Path, list[_IgnoreRule]] = {}
        if respect_gitignore:
            self._load_gitignores()

    # ---- public API ------------------------------------------------------

    def iter_files(self) -> Iterator[Path]:
        yield from self._walk(self.root)

    def accept(self, path: Path) -> bool:
        """True if ``path`` should be ingested, honoring all rules."""
        rel = self._relative(path)
        if rel is None:
            return False
        if self._is_in_excluded_dir(rel):
            return False
        if self.excludes and _matches_any(rel, self.excludes):
            return False
        if self.includes and not _matches_any(rel, self.includes):
            return False
        if self._gitignore_matches(rel):
            return False
        return True

    # ---- internals -------------------------------------------------------

    def _walk(self, directory: Path) -> Iterator[Path]:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except (FileNotFoundError, PermissionError):
            return
        for entry in entries:
            if entry.is_symlink() and not self.follow_symlinks:
                continue
            if entry.is_dir():
                if entry.name in DEFAULT_EXCLUDED_DIRS:
                    continue
                rel = self._relative(entry)
                if rel is not None and self._gitignore_matches(rel, is_dir=True):
                    continue
                if rel is not None and self.excludes and _matches_any(
                    rel, self.excludes, is_dir=True
                ):
                    continue
                yield from self._walk(entry)
            elif entry.is_file():
                if self.accept(entry):
                    yield entry

    def _relative(self, path: Path) -> Optional[PurePosixPath]:
        try:
            rel = path.resolve().relative_to(self.root)
        except ValueError:
            return None
        return PurePosixPath(*rel.parts)

    def _is_in_excluded_dir(self, rel: PurePosixPath) -> bool:
        return any(part in DEFAULT_EXCLUDED_DIRS for part in rel.parts[:-1])

    # ---- gitignore loader ------------------------------------------------

    def _load_gitignores(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=self.follow_symlinks):
            dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDED_DIRS]
            if ".gitignore" in filenames:
                scope = Path(dirpath).resolve()
                rules = _parse_gitignore((scope / ".gitignore").read_text(encoding="utf-8"))
                if rules:
                    self._gitignores[scope] = rules

    def _gitignore_matches(self, rel: PurePosixPath, *, is_dir: bool = False) -> bool:
        # Git applies .gitignore files shallow-to-deep; each rule either
        # ignores or un-ignores, and the last matching rule wins. That
        # lets a nested ``!pattern`` override an outer ``pattern``.
        matched = False
        for scope, rules in sorted(
            self._gitignores.items(), key=lambda kv: len(kv[0].parts)
        ):
            try:
                scope_rel = rel.relative_to(
                    PurePosixPath(*scope.relative_to(self.root).parts)
                )
            except ValueError:
                continue
            for rule in rules:
                if rule.matches(scope_rel, is_dir=is_dir):
                    matched = not rule.negated
        return matched


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


def _matches_any(rel: PurePosixPath, patterns: list[str], *, is_dir: bool = False) -> bool:
    rel_s = rel.as_posix()
    for pat in patterns:
        if _single_match(rel_s, pat, is_dir=is_dir):
            return True
    return False


def _single_match(rel_s: str, pattern: str, *, is_dir: bool) -> bool:
    # fnmatch handles `*`, `?`, and `**` when paths use forward slashes.
    if fnmatch.fnmatch(rel_s, pattern):
        return True
    if "**" in pattern:
        regex = _glob_to_regex(pattern)
        if regex.match(rel_s):
            return True
    return False


_GLOB_TO_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    cached = _GLOB_TO_REGEX_CACHE.get(pattern)
    if cached:
        return cached
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i):
            out.append("(?:/.*)?")
            i += 3
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == ".":
            out.append("\\.")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    regex = re.compile("^" + "".join(out) + "$")
    _GLOB_TO_REGEX_CACHE[pattern] = regex
    return regex


# ---------------------------------------------------------------------------
# Minimal .gitignore parser
# ---------------------------------------------------------------------------


class _IgnoreRule:
    __slots__ = ("pattern", "negated", "dir_only", "anchored")

    def __init__(self, pattern: str, *, negated: bool, dir_only: bool, anchored: bool):
        self.pattern = pattern
        self.negated = negated
        self.dir_only = dir_only
        self.anchored = anchored

    def matches(self, rel: PurePosixPath, *, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        rel_s = rel.as_posix()
        pat = self.pattern
        if self.anchored:
            return _single_match(rel_s, pat, is_dir=is_dir)
        # Unanchored: match against any suffix of the path.
        parts = rel.parts
        for i in range(len(parts)):
            candidate = "/".join(parts[i:])
            if _single_match(candidate, pat, is_dir=is_dir):
                return True
        return False


def _parse_gitignore(text: str) -> list[_IgnoreRule]:
    rules: list[_IgnoreRule] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line or line.startswith("#"):
            continue
        negated = False
        if line.startswith("!"):
            negated = True
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        if not line:
            continue
        rules.append(
            _IgnoreRule(
                pattern=line,
                negated=negated,
                dir_only=dir_only,
                anchored=anchored,
            )
        )
    return rules


__all__ = ["build_walker", "Walker", "DEFAULT_EXCLUDED_DIRS"]
