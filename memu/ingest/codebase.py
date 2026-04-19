"""Codebase ingestion orchestrator.

Walks a source tree, parses each file with the right language parser,
resolves cross-file references into wikilinks, and writes one
:class:`WikiNode` per discovered symbol (module, class, function,
method) into the active :class:`StorageBackend`.

The ingest is **idempotent**: re-running against an unchanged tree
produces zero writes. Incremental mode uses a SHA-256 content hash
stored in each node's frontmatter; if the hash matches, we keep the
existing node and skip.

Design notes
------------
* One ingest = one commit. Callers provide ``commit_sha`` (or we try to
  read ``.git/HEAD``). Each emitted node records it in
  ``source.commit`` so later tooling can diff wiki-vs-source.
* Symbols that vanish in a re-ingest are left alone. We do **not**
  delete nodes automatically — users curate the vault, and silent
  deletions would nuke handwritten notes that happen to live at the
  same slug.  A ``vault-doctor`` pass can surface stale nodes later.
* Resolution is best-effort. Unresolved references become plain text in
  the body, never fake wikilinks.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from ..storage.base import LinkRecord, StorageBackend, WikiNode
from .filters import Walker, build_walker
from .parsers import ParsedFile, ParsedSymbol, detect_language, get_parser
from .resolve import LinkResolver, ResolvedLink, module_to_slug


MAX_BODY_LINES = 400
"""Source snippets longer than this get head+tail with an elision marker.
Covers the 99th percentile of real-world function sizes without bloating
markdown files."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped_files: int = 0
    scanned_files: int = 0
    parse_errors: int = 0
    elapsed_ms: float = 0.0
    root: str = ""
    commit_sha: Optional[str] = None

    def summary(self) -> str:
        return (
            f"added={len(self.added)} updated={len(self.updated)} "
            f"unchanged={len(self.unchanged)} scanned={self.scanned_files} "
            f"skipped={self.skipped_files} errors={self.parse_errors} "
            f"elapsed={self.elapsed_ms:.0f}ms"
        )


async def ingest_codebase(
    backend: StorageBackend,
    root: Path | str,
    *,
    languages: Optional[list[str]] = None,
    includes: Optional[list[str]] = None,
    excludes: Optional[list[str]] = None,
    respect_gitignore: bool = True,
    commit_sha: Optional[str] = None,
    repo_url: Optional[str] = None,
    agent_id: str = "ingest.codebase",
    force: bool = False,
) -> IngestResult:
    """Ingest ``root`` into ``backend``.

    Parameters
    ----------
    languages:
        Restrict to these parser keys (``["python"]``). ``None`` means
        every supported language.
    includes / excludes:
        Extra glob filters applied on top of the default blocklist.
    respect_gitignore:
        Honor ``.gitignore`` files encountered during the walk.
    commit_sha / repo_url:
        Recorded in each node's ``source`` frontmatter for traceability.
        If ``commit_sha`` is ``None`` we try ``git rev-parse HEAD``.
    force:
        Rewrite every node even when the content hash matches.
    """
    started = time.perf_counter()
    root_path = Path(root).expanduser().resolve()
    result = IngestResult(root=str(root_path))
    result.commit_sha = commit_sha or _read_commit_sha(root_path)

    walker = build_walker(
        root_path,
        includes=includes,
        excludes=excludes,
        respect_gitignore=respect_gitignore,
    )
    parsed_files = list(_parse_all(walker, root_path, languages, result))

    if not parsed_files:
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        return result

    resolver = LinkResolver(parsed_files)

    for parsed in parsed_files:
        await _emit_module_node(
            backend,
            parsed,
            result=result,
            resolver=resolver,
            commit_sha=result.commit_sha,
            repo_url=repo_url,
            agent_id=agent_id,
            force=force,
        )
        for symbol in parsed.symbols:
            await _emit_symbol_node(
                backend,
                parsed,
                symbol,
                result=result,
                resolver=resolver,
                commit_sha=result.commit_sha,
                repo_url=repo_url,
                agent_id=agent_id,
                force=force,
            )

    result.elapsed_ms = (time.perf_counter() - started) * 1000
    return result


# ---------------------------------------------------------------------------
# Parsing pipeline
# ---------------------------------------------------------------------------


def _parse_all(
    walker: Walker,
    root: Path,
    languages: Optional[list[str]],
    result: IngestResult,
) -> Iterable[ParsedFile]:
    allowed = set(languages) if languages else None
    # If the root directory is itself a Python package, use its name as
    # the implicit top-level module prefix so slugs carry the package
    # name (``ingest memu/wiki`` -> ``code/wiki/...``). Users who want
    # their package rooted at ``<root>/<pkg>/__init__.py`` just point
    # memU at the parent directory.
    root_package_name: Optional[str] = (
        root.name if (root / "__init__.py").exists() else None
    )
    for path in walker.iter_files():
        language = detect_language(path.suffix)
        if language is None:
            continue
        if allowed is not None and language not in allowed:
            continue
        parser = get_parser(language)
        if parser is None:
            continue
        rel = path.resolve().relative_to(root)
        rel_posix = PurePosixPath(*rel.parts)
        module_fqn = _module_fqn_for(rel, root_package_name=root_package_name)
        result.scanned_files += 1
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            result.skipped_files += 1
            continue
        parsed = parser.parse(source, rel_path=Path(str(rel_posix)), module_fqn=module_fqn)
        parsed.path = path
        if not parsed.symbols and parsed.module_docstring is None:
            # Unparseable or empty; count once as a skipped file.
            if parsed.content_hash and not parsed.symbols and not parsed.module_docstring:
                # Could be an intentionally empty file; still emit a module node.
                pass
        if not parsed.symbols and parsed.module_docstring is None and not parsed.imports:
            # Truly nothing to index — likely syntax error or empty.
            if _looks_like_empty(path):
                yield parsed
            else:
                result.parse_errors += 1
                continue
        yield parsed


def _module_fqn_for(rel: Path, *, root_package_name: Optional[str] = None) -> str:
    """Convert a relative POSIX-ish path into a Python dotted name.

    ``pkg/sub/__init__.py`` -> ``pkg.sub``.
    ``pkg/sub/mod.py``      -> ``pkg.sub.mod``.

    When ``root_package_name`` is supplied (because the ingestion root is
    itself a package), it is prepended so the top-level ``__init__.py``
    becomes ``<root_package_name>`` rather than the empty string.
    """
    parts = list(rel.parts)
    if not parts:
        return root_package_name or ""
    last = parts[-1]
    stem = last.rsplit(".", 1)[0]
    if stem == "__init__":
        parts = parts[:-1]
    else:
        parts[-1] = stem
    if root_package_name:
        return ".".join([root_package_name, *parts]) if parts else root_package_name
    return ".".join(parts)


def _looks_like_empty(path: Path) -> bool:
    try:
        return path.stat().st_size <= 2
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Node emission
# ---------------------------------------------------------------------------


async def _emit_module_node(
    backend: StorageBackend,
    parsed: ParsedFile,
    *,
    result: IngestResult,
    resolver: LinkResolver,
    commit_sha: Optional[str],
    repo_url: Optional[str],
    agent_id: str,
    force: bool,
) -> None:
    slug = module_to_slug(parsed.module_fqn)
    import_links = _import_only_links(parsed, resolver)
    body = _render_module_body(parsed, import_links)
    title = f"{parsed.module_fqn} (module)" if parsed.module_fqn else str(parsed.rel_path)
    node = _build_node(
        slug=slug,
        title=title,
        kind="code",
        body=body,
        links=import_links,
        parsed=parsed,
        symbol=None,
        commit_sha=commit_sha,
        repo_url=repo_url,
        agent_id=agent_id,
    )
    await _put_with_hash(backend, node, parsed.content_hash, result, force=force)


async def _emit_symbol_node(
    backend: StorageBackend,
    parsed: ParsedFile,
    symbol: ParsedSymbol,
    *,
    result: IngestResult,
    resolver: LinkResolver,
    commit_sha: Optional[str],
    repo_url: Optional[str],
    agent_id: str,
    force: bool,
) -> None:
    links_by_qual = resolver.resolve_file(parsed)
    resolved = links_by_qual.get(symbol.qualname, [])
    slug = module_to_slug(parsed.module_fqn, symbol.qualname)
    body = _render_symbol_body(parsed, symbol, resolved)
    title = f"{parsed.module_fqn}.{symbol.qualname}"
    link_records = [
        LinkRecord(
            src_slug=slug,
            dst_slug=link.dst_slug,
            type="related",
            strength=0.6,
        )
        for link in resolved
    ]
    # Parent-of link for methods: method -> its class.
    if symbol.kind == "method" and "." in symbol.qualname:
        class_name = symbol.qualname.split(".", 1)[0]
        parent_slug = module_to_slug(parsed.module_fqn, class_name)
        if parent_slug != slug:
            link_records.append(
                LinkRecord(
                    src_slug=slug,
                    dst_slug=parent_slug,
                    type="extends",
                    strength=0.8,
                )
            )
    node = _build_node(
        slug=slug,
        title=title,
        kind="code",
        body=body,
        links=link_records,
        parsed=parsed,
        symbol=symbol,
        commit_sha=commit_sha,
        repo_url=repo_url,
        agent_id=agent_id,
    )
    await _put_with_hash(backend, node, parsed.content_hash, result, force=force)


def _import_only_links(
    parsed: ParsedFile, resolver: LinkResolver
) -> list[LinkRecord]:
    """Synthesize module-level links from the file's imports.

    The module node itself has no references array, so we take the union
    of its imported modules that are also ingested.
    """
    slug = module_to_slug(parsed.module_fqn)
    seen: set[str] = set()
    out: list[LinkRecord] = []
    for imp in parsed.imports:
        if imp.module in resolver._files_by_module:  # noqa: SLF001
            dst = module_to_slug(imp.module)
            if dst != slug and dst not in seen:
                seen.add(dst)
                out.append(
                    LinkRecord(
                        src_slug=slug,
                        dst_slug=dst,
                        type="related",
                        strength=0.5,
                    )
                )
    return out


def _build_node(
    *,
    slug: str,
    title: str,
    kind: str,
    body: str,
    links: list[LinkRecord],
    parsed: ParsedFile,
    symbol: Optional[ParsedSymbol],
    commit_sha: Optional[str],
    repo_url: Optional[str],
    agent_id: str,
) -> WikiNode:
    tags = ["code", parsed.language]
    if symbol is not None:
        tags.append(symbol.kind)
        if symbol.is_private:
            tags.append("private")
    source: dict[str, Any] = {
        "path": parsed.rel_path.as_posix() if hasattr(parsed.rel_path, "as_posix") else str(parsed.rel_path),
        "language": parsed.language,
        "module": parsed.module_fqn,
    }
    if symbol is not None:
        source["symbol"] = symbol.qualname
        source["start_line"] = symbol.start_line
        source["end_line"] = symbol.end_line
        if symbol.signature:
            source["signature"] = symbol.signature
    if commit_sha:
        source["commit"] = commit_sha
    if repo_url:
        source["repo"] = repo_url
    # ``source_hash`` — SHA-256 over the raw source file, powering the
    # ingester's "skip unchanged" fast path. Distinct from
    # :meth:`WikiNode.content_hash` (which hashes the rendered wiki
    # body+title for reinforcement-on-duplicate). Namespaced to avoid
    # collision when both concerns coexist.
    metadata = {"source_hash": parsed.content_hash}
    return WikiNode(
        id=str(uuid.uuid4()),
        slug=slug,
        kind=kind,
        title=title,
        body=body,
        tags=tags,
        links=links,
        metadata=metadata,
        agent_id=agent_id,
        memory_type="observation",
        salience=0.5,
        confidence=0.9,
        source=source,
        created_at=datetime.now(timezone.utc),
    )


async def _put_with_hash(
    backend: StorageBackend,
    node: WikiNode,
    source_hash: str,
    result: IngestResult,
    *,
    force: bool,
) -> None:
    """Skip-if-unchanged helper keyed on the raw-source-file hash.

    ``source_hash`` is the ingester's SHA-256 of the file bytes. It
    lives in ``metadata['source_hash']`` and is independent of
    :meth:`WikiNode.content_hash`, which hashes the rendered wiki body
    and drives reinforcement-on-duplicate inside the backend's
    ``put_node``.
    """
    existing = await backend.get_node(node.slug)
    if existing is not None:
        meta = existing.metadata or {}
        # Accept both the new ``source_hash`` key and the legacy
        # ``content_hash`` key so vaults built before the rename stay
        # incremental across an upgrade.
        existing_hash = meta.get("source_hash") or meta.get("content_hash")
        if not force and existing_hash == source_hash:
            result.unchanged.append(node.slug)
            return
        node.id = existing.id
        node.created_at = existing.created_at
        await backend.put_node(node)
        result.updated.append(node.slug)
        return
    await backend.put_node(node)
    result.added.append(node.slug)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_module_body(
    parsed: ParsedFile, import_links: list[LinkRecord]
) -> str:
    lines: list[str] = [f"# `{parsed.module_fqn}`", ""]
    if parsed.module_docstring:
        lines.extend((parsed.module_docstring.strip(), ""))
    if parsed.symbols:
        lines.append("## Symbols")
        for symbol in parsed.symbols:
            sig = _ellipsize(symbol.signature, 120) if symbol.signature else symbol.qualname
            link = f"[[{module_to_slug(parsed.module_fqn, symbol.qualname)}|{symbol.qualname}]]"
            lines.append(f"- {link} — `{sig}`")
        lines.append("")
    if import_links:
        lines.append("## Imports")
        for link in import_links:
            lines.append(f"- [[{link.dst_slug}]]")
        lines.append("")
    lines.append(f"_Source:_ `{parsed.rel_path}`")
    return "\n".join(lines) + "\n"


def _render_symbol_body(
    parsed: ParsedFile, symbol: ParsedSymbol, resolved: list[ResolvedLink]
) -> str:
    lines: list[str] = [f"# `{symbol.qualname}`", ""]
    if symbol.signature:
        lines.extend(("```python", symbol.signature, "```", ""))
    if symbol.docstring:
        lines.extend((symbol.docstring.strip(), ""))
    if symbol.body_snippet:
        snippet = _truncate_snippet(symbol.body_snippet)
        lines.extend(("```python", snippet.rstrip(), "```", ""))
    if resolved:
        lines.append("## References")
        for link in resolved:
            short = _short_dst(link)
            lines.append(f"- [[{link.dst_slug}|{short}]] (line {link.line})")
        lines.append("")
    location = (
        f"{parsed.rel_path}:{symbol.start_line}"
        if symbol.start_line
        else str(parsed.rel_path)
    )
    lines.append(f"_Source:_ `{location}`")
    return "\n".join(lines) + "\n"


def _short_dst(link: ResolvedLink) -> str:
    if link.dst_name:
        return f"{link.dst_module_fqn}.{link.dst_name}"
    return link.dst_module_fqn


def _ellipsize(text: str, width: int) -> str:
    text = text.strip()
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def _truncate_snippet(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= MAX_BODY_LINES:
        return text
    head = lines[: MAX_BODY_LINES // 2]
    tail = lines[-MAX_BODY_LINES // 2 :]
    elision = f"\n# ... ({len(lines) - MAX_BODY_LINES} lines elided) ...\n"
    return "\n".join(head) + elision + "\n".join(tail)


# ---------------------------------------------------------------------------
# Commit detection
# ---------------------------------------------------------------------------


_HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _read_commit_sha(root: Path) -> Optional[str]:
    """Best-effort ``git rev-parse HEAD`` without shelling out.

    Walks up from ``root`` looking for a ``.git`` entry; handles both
    normal repos (``.git`` is a dir) and git worktrees (``.git`` is a
    file pointing at the real gitdir).
    """
    git_dir = _find_git_dir(root)
    if git_dir is None:
        return None
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        return None
    try:
        head = head_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if head.startswith("ref: "):
        ref = head[len("ref: ") :]
        ref_path = git_dir / ref
        try:
            return ref_path.read_text(encoding="utf-8").strip()
        except OSError:
            # Packed-refs fallback.
            packed = git_dir / "packed-refs"
            if packed.exists():
                try:
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        if line.endswith(" " + ref):
                            return line.split()[0]
                except OSError:
                    return None
            return None
    if _HEX_RE.match(head):
        return head
    return None


def _find_git_dir(start: Path) -> Optional[Path]:
    for candidate in (start, *start.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            try:
                txt = dot_git.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if txt.startswith("gitdir: "):
                return (candidate / txt[len("gitdir: ") :]).resolve()
    return None


__all__ = ["ingest_codebase", "IngestResult"]
