"""Cross-file link resolution.

Takes a list of :class:`ParsedFile` records and turns the unresolved
``Reference`` objects inside each symbol into wiki links pointing at
other slugs in the same ingested set. Only references we can *prove*
point at an ingested symbol become links — we never invent edges just
because a name could plausibly refer to something.

Resolution is purely lexical: we do not evaluate code, do not look at
third-party packages, and do not attempt type inference. The upside is
speed and predictability; the downside is that dynamic wiring (e.g.
``getattr`` tricks, DI containers) is invisible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .parsers import Import, ParsedFile, ParsedSymbol, Reference


@dataclass(frozen=True)
class ResolvedLink:
    """A resolved outbound edge from ``src_qualname`` in
    ``src_module_fqn`` to a target identified by ``dst_slug``."""

    src_module_fqn: str
    src_qualname: str
    dst_slug: str
    dst_module_fqn: str
    dst_name: str
    line: int


class LinkResolver:
    """Resolves :class:`Reference` values against an ingested symbol table.

    Construct once per ingestion run; call :meth:`resolve_file` or
    :meth:`resolve_symbol` as many times as needed. The resolver is
    stateless across calls (no caches mutate), so a single instance is
    safe to share.
    """

    def __init__(self, files: list[ParsedFile]):
        self._files_by_module: dict[str, ParsedFile] = {f.module_fqn: f for f in files}
        self._symbols_by_module: dict[str, frozenset[str]] = {}
        self._top_level_by_module: dict[str, frozenset[str]] = {}
        for f in files:
            qualnames = {s.qualname for s in f.symbols}
            self._symbols_by_module[f.module_fqn] = frozenset(qualnames)
            self._top_level_by_module[f.module_fqn] = frozenset(
                q.split(".", 1)[0] for q in qualnames
            )

    # ---- public API ------------------------------------------------------

    def resolve_file(self, file: ParsedFile) -> dict[str, list[ResolvedLink]]:
        """Return a map of ``qualname -> [ResolvedLink, ...]`` for ``file``.

        Links are deduped by ``(dst_slug,)`` per source symbol so we don't
        render the same backlink five times for a chatty function.
        """
        known_modules = frozenset(self._files_by_module.keys())
        alias_table = _build_alias_table(file.imports, known_modules=known_modules)
        out: dict[str, list[ResolvedLink]] = {}
        same_module_tops = self._top_level_by_module.get(file.module_fqn, frozenset())
        for symbol in file.symbols:
            seen: set[str] = set()
            resolved: list[ResolvedLink] = []
            for ref in symbol.references:
                link = self._resolve_one(
                    ref=ref,
                    symbol=symbol,
                    file=file,
                    alias_table=alias_table,
                    same_module_tops=same_module_tops,
                )
                if link is None or link.dst_slug in seen:
                    continue
                seen.add(link.dst_slug)
                resolved.append(link)
            if resolved:
                out[symbol.qualname] = resolved
        return out

    def resolve_symbol(
        self,
        file: ParsedFile,
        symbol: ParsedSymbol,
    ) -> list[ResolvedLink]:
        return self.resolve_file(file).get(symbol.qualname, [])

    # ---- internals -------------------------------------------------------

    def _resolve_one(
        self,
        *,
        ref: Reference,
        symbol: ParsedSymbol,
        file: ParsedFile,
        alias_table: "_AliasTable",
        same_module_tops: frozenset[str],
    ) -> Optional[ResolvedLink]:
        parts = ref.name.split(".")
        head = parts[0]

        # 1) Class-local `self.<method>` hints emitted by the parser arrive
        #    pre-resolved — the parser strips the ``self.`` and passes the
        #    method name only. We detect this because the current symbol is
        #    a method and the reference matches a sibling's short name.
        if symbol.kind == "method" and len(parts) == 1 and "." in symbol.qualname:
            class_name = symbol.qualname.split(".", 1)[0]
            sibling_qual = f"{class_name}.{head}"
            if sibling_qual in self._symbols_by_module.get(file.module_fqn, frozenset()):
                return self._make_link(
                    symbol=symbol,
                    file=file,
                    dst_module=file.module_fqn,
                    dst_name=sibling_qual,
                    line=ref.line,
                )

        # 2) Import alias match (longest-prefix wins).
        match = alias_table.best_match(parts)
        if match is not None:
            module, target_parts = match
            dst_name = ".".join(target_parts) if target_parts else ""
            return self._maybe_link(
                symbol=symbol,
                file=file,
                dst_module=module,
                dst_name=dst_name,
                line=ref.line,
            )

        # 3) Same-module top-level reference.
        if head in same_module_tops:
            dst_name = _longest_symbol_prefix(
                parts, self._symbols_by_module[file.module_fqn]
            )
            if dst_name:
                return self._make_link(
                    symbol=symbol,
                    file=file,
                    dst_module=file.module_fqn,
                    dst_name=dst_name,
                    line=ref.line,
                )

        # 4) Fully-qualified module path (e.g. ``pkg.mod.Foo``).
        for i in range(len(parts), 0, -1):
            candidate_module = ".".join(parts[:i])
            if candidate_module in self._files_by_module:
                remainder = parts[i:]
                dst_name = _longest_symbol_prefix(
                    remainder, self._symbols_by_module[candidate_module]
                )
                if dst_name or not remainder:
                    return self._make_link(
                        symbol=symbol,
                        file=file,
                        dst_module=candidate_module,
                        dst_name=dst_name,
                        line=ref.line,
                    )
        return None

    def _maybe_link(
        self,
        *,
        symbol: ParsedSymbol,
        file: ParsedFile,
        dst_module: str,
        dst_name: str,
        line: int,
    ) -> Optional[ResolvedLink]:
        if dst_module not in self._files_by_module:
            return None
        # If a specific symbol was requested but isn't present in the
        # target module, fall back to the module-level node.
        if dst_name and dst_name not in self._symbols_by_module[dst_module]:
            head = dst_name.split(".", 1)[0]
            if head in self._symbols_by_module[dst_module]:
                dst_name = head
            else:
                dst_name = ""
        return self._make_link(
            symbol=symbol,
            file=file,
            dst_module=dst_module,
            dst_name=dst_name,
            line=line,
        )

    def _make_link(
        self,
        *,
        symbol: ParsedSymbol,
        file: ParsedFile,
        dst_module: str,
        dst_name: str,
        line: int,
    ) -> Optional[ResolvedLink]:
        dst_slug = module_to_slug(dst_module, dst_name)
        src_slug = module_to_slug(file.module_fqn, symbol.qualname)
        if dst_slug == src_slug:
            return None  # self-link noise
        return ResolvedLink(
            src_module_fqn=file.module_fqn,
            src_qualname=symbol.qualname,
            dst_slug=dst_slug,
            dst_module_fqn=dst_module,
            dst_name=dst_name,
            line=line,
        )


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def module_to_slug(module_fqn: str, symbol_qualname: str = "") -> str:
    module_path = module_fqn.replace(".", "/")
    if not symbol_qualname:
        return f"code/{module_path}"
    return f"code/{module_path}/{symbol_qualname}"


def _longest_symbol_prefix(parts: list[str], known: frozenset[str]) -> str:
    """Return the longest dotted prefix of ``parts`` that is in ``known``,
    or ``""`` if none match."""
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in known:
            return candidate
    return ""


# ---------------------------------------------------------------------------
# Alias tables
# ---------------------------------------------------------------------------


class _AliasTable:
    """Maps a local alias (the name as used in the source) to the
    ``(module_fqn, [target_parts])`` pair it resolves to.

    Examples::

        from pkg.mod import Foo          # alias "Foo"   -> ("pkg.mod", ["Foo"])
        from pkg.mod import Foo as F     # alias "F"     -> ("pkg.mod", ["Foo"])
        import pkg.mod                   # alias "pkg"   -> ("pkg.mod", [])
                                         # alias "pkg.mod" -> ("pkg.mod", [])
        import pkg.mod as pm             # alias "pm"    -> ("pkg.mod", [])
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, list[str]]] = {}

    def add(self, local: str, module: str, target: list[str]) -> None:
        if local and local not in self._entries:
            self._entries[local] = (module, list(target))

    def best_match(self, parts: list[str]) -> Optional[tuple[str, list[str]]]:
        """Longest-prefix match against the alias table.

        Returns ``(module_fqn, target_parts_after_alias)`` where
        ``target_parts_after_alias`` concatenates the alias's bound
        target with the remainder of ``parts`` past the alias.
        """
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            entry = self._entries.get(candidate)
            if entry:
                module, bound = entry
                remainder = parts[i:]
                return module, list(bound) + list(remainder)
        return None


def _build_alias_table(
    imports: Iterable[Import],
    *,
    known_modules: frozenset[str] = frozenset(),
) -> _AliasTable:
    """Build the per-file alias lookup used during reference resolution.

    ``known_modules`` is the set of module FQNs already ingested. It lets
    us disambiguate ``from pkg import utils`` cases where ``utils`` is a
    submodule of ``pkg`` (bind alias to the module, not to a symbol
    ``utils`` inside ``pkg``).
    """
    table = _AliasTable()
    for imp in imports:
        if not imp.names:
            continue
        # ``import pkg.mod [as alias]`` is represented as a single
        # ("", alias-or-dotted-name) pair by the parser.
        if len(imp.names) == 1 and imp.names[0][0] == "":
            alias = imp.names[0][1]
            table.add(alias, imp.module, [])
            if alias == imp.module:
                # Bare ``import pkg.mod`` — also register the full dotted
                # path for completeness.
                table.add(imp.module, imp.module, [])
            continue
        # ``from pkg.mod import X [as Y]``
        for orig, alias in imp.names:
            if not alias:
                continue
            submodule = f"{imp.module}.{orig}" if imp.module else orig
            if submodule in known_modules:
                # Submodule import: the alias refers to the module itself.
                table.add(alias, submodule, [])
            else:
                table.add(alias, imp.module, [orig])
    return table


__all__ = ["LinkResolver", "ResolvedLink", "module_to_slug"]
