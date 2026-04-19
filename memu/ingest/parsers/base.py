"""Parser contract: language-agnostic types that the resolver can consume.

A parser reads source text and emits a :class:`ParsedFile` describing:

* the symbols it defines (classes, functions, methods, module-level)
* the imports it uses (for cross-file resolution)
* unresolved references from each symbol's body (free names and
  attribute access) which the resolver will later turn into wikilinks.

Parsers are intentionally dumb about the rest of the tree: they see one
file at a time. All cross-file name resolution lives in
``memu.ingest.resolve``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Protocol


SymbolKind = Literal["module", "class", "function", "method"]


@dataclass
class Reference:
    """A name usage inside a symbol's body.

    ``name`` is the shortest expression as written in the source — e.g.
    ``Foo`` for ``Foo(...)`` or ``utils.helper`` for
    ``utils.helper(...)``. The resolver splits on dots and looks up each
    prefix against imports + module symbols.
    """

    name: str
    line: int


@dataclass
class Import:
    """A single ``import`` or ``from ... import ...`` clause.

    ``module`` is the fully-qualified module path as resolved against
    the current package (relative imports are normalized). ``names`` is
    the list of ``(imported_name, local_alias)`` pairs; for plain
    ``import foo.bar`` there is exactly one pair ``("", "foo.bar")``.
    """

    module: str
    names: list[tuple[str, str]]
    is_relative: bool = False
    line: int = 0


@dataclass
class ParsedSymbol:
    qualname: str
    """Dotted name relative to the module. E.g. ``Foo`` or ``Foo.bar``."""

    kind: SymbolKind
    start_line: int
    end_line: int
    signature: str
    docstring: Optional[str] = None
    body_snippet: str = ""
    references: list[Reference] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    is_private: bool = False
    """True if any path component of ``qualname`` starts with ``_``.
    Callers decide whether to filter; the parser does not."""


@dataclass
class ParsedFile:
    path: Path
    """Absolute path to the source file."""

    rel_path: Path
    """Path relative to the ingestion root, in POSIX form."""

    module_fqn: str
    """Fully-qualified module name (``tiny_pkg.core``). Derived by the
    orchestrator before parsing."""

    language: str
    content_hash: str
    """SHA-256 of the raw source bytes; used for incremental ingestion."""

    module_docstring: Optional[str] = None
    imports: list[Import] = field(default_factory=list)
    symbols: list[ParsedSymbol] = field(default_factory=list)


class LanguageParser(Protocol):
    """Synchronous parser — source is already in memory by this point."""

    def parse(self, source: str, *, rel_path: Path, module_fqn: str) -> ParsedFile: ...
