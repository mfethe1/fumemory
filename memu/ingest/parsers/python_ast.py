"""Python parser built on the stdlib :mod:`ast` module.

Deliberately conservative: we extract top-level classes and functions,
plus the methods of each top-level class. Nested functions and classes
nested inside functions are *not* emitted as standalone symbols — they
live inside their parent's body snippet instead. This keeps the wiki
graph human-scale.

Reference extraction tracks, per symbol:

* every free name (``Foo(...)``) that is *not* a builtin and not a
  locally-bound variable,
* every dotted attribute access (``utils.helper``, ``self.bar``) whose
  root is likewise not local,
* ``self.<method>`` calls, which the resolver rewrites into
  ``ClassName.<method>`` links.

We intentionally do not resolve names here — that lives in
``memu.ingest.resolve`` where cross-file context is available.
"""
from __future__ import annotations

import ast
import builtins
import hashlib
from pathlib import Path
from typing import Iterable, Optional

from .base import Import, ParsedFile, ParsedSymbol, Reference


_BUILTINS: frozenset[str] = frozenset(dir(builtins))


class PythonAstParser:
    language = "python"

    def parse(
        self,
        source: str,
        *,
        rel_path: Path,
        module_fqn: str,
    ) -> ParsedFile:
        content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        try:
            tree = ast.parse(source, filename=str(rel_path))
        except SyntaxError:
            # Emit an otherwise-empty ParsedFile so callers can still
            # surface the file as "scanned but unusable".
            return ParsedFile(
                path=Path(),
                rel_path=rel_path,
                module_fqn=module_fqn,
                language="python",
                content_hash=content_hash,
                module_docstring=None,
            )

        source_lines = source.splitlines(keepends=True)
        module_docstring = ast.get_docstring(tree)
        imports = list(_extract_imports(tree, module_fqn))
        symbols = list(_extract_symbols(tree, source_lines))

        return ParsedFile(
            path=Path(),
            rel_path=rel_path,
            module_fqn=module_fqn,
            language="python",
            content_hash=content_hash,
            module_docstring=module_docstring,
            imports=imports,
            symbols=symbols,
        )


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------


def _extract_imports(tree: ast.Module, module_fqn: str) -> Iterable[Import]:
    package = _package_of(module_fqn)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield Import(
                    module=alias.name,
                    names=[("", alias.asname or alias.name)],
                    is_relative=False,
                    line=node.lineno,
                )
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_relative(node.module or "", node.level, package)
            if not module:
                continue
            pairs: list[tuple[str, str]] = []
            for alias in node.names:
                if alias.name == "*":
                    continue
                pairs.append((alias.name, alias.asname or alias.name))
            if pairs:
                yield Import(
                    module=module,
                    names=pairs,
                    is_relative=node.level > 0,
                    line=node.lineno,
                )


def _package_of(module_fqn: str) -> str:
    if "." not in module_fqn:
        return ""
    return module_fqn.rsplit(".", 1)[0]


def _resolve_relative(module: str, level: int, package: str) -> str:
    """Turn ``(module='utils', level=1, package='pkg.sub')`` into ``pkg.sub.utils``."""
    if level == 0:
        return module
    parts = package.split(".") if package else []
    if level > len(parts):
        return module  # over-dotted — fall back gracefully
    base = ".".join(parts[: len(parts) - (level - 1)])
    if module:
        return f"{base}.{module}" if base else module
    return base


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


def _extract_symbols(
    tree: ast.Module, source_lines: list[str]
) -> Iterable[ParsedSymbol]:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield _symbol_from_function(node, source_lines, prefix="", kind="function")
        elif isinstance(node, ast.ClassDef):
            yield from _symbols_from_class(node, source_lines)


def _symbols_from_class(
    cls: ast.ClassDef, source_lines: list[str]
) -> Iterable[ParsedSymbol]:
    # The class itself
    class_refs = _collect_references(
        cls,
        local_names=_class_local_names(cls),
        skip_nested=True,
    )
    yield ParsedSymbol(
        qualname=cls.name,
        kind="class",
        start_line=cls.lineno,
        end_line=_end_line(cls, source_lines),
        signature=_class_signature(cls),
        docstring=ast.get_docstring(cls),
        body_snippet=_slice_source(cls, source_lines),
        references=class_refs,
        decorators=[ast.unparse(d) for d in cls.decorator_list],
        is_private=cls.name.startswith("_"),
    )
    # Its methods
    method_names = _class_method_names(cls)
    for child in cls.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield _symbol_from_function(
                child,
                source_lines,
                prefix=f"{cls.name}.",
                kind="method",
                sibling_methods=method_names,
            )


def _symbol_from_function(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    source_lines: list[str],
    *,
    prefix: str,
    kind: str,
    sibling_methods: Optional[frozenset[str]] = None,
) -> ParsedSymbol:
    qualname = f"{prefix}{fn.name}"
    params = {arg.arg for arg in fn.args.args}
    params.update(arg.arg for arg in fn.args.kwonlyargs)
    if fn.args.vararg:
        params.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        params.add(fn.args.kwarg.arg)
    references = _collect_references(
        fn,
        local_names=_assigned_names(fn) | params,
        sibling_methods=sibling_methods,
    )
    return ParsedSymbol(
        qualname=qualname,
        kind="method" if kind == "method" else "function",
        start_line=fn.lineno,
        end_line=_end_line(fn, source_lines),
        signature=_function_signature(fn),
        docstring=ast.get_docstring(fn),
        body_snippet=_slice_source(fn, source_lines),
        references=references,
        decorators=[ast.unparse(d) for d in fn.decorator_list],
        is_private=any(p.startswith("_") and p != "__init__" for p in qualname.split(".")),
    )


def _class_local_names(cls: ast.ClassDef) -> frozenset[str]:
    names: set[str] = set()
    for child in cls.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(child.name)
        elif isinstance(child, ast.ClassDef):
            names.add(child.name)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            names.add(child.target.id)
    return frozenset(names)


def _class_method_names(cls: ast.ClassDef) -> frozenset[str]:
    return frozenset(
        child.name
        for child in cls.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _assigned_names(node: ast.AST) -> frozenset[str]:
    """Names bound by assignment or ``for`` target inside ``node``'s body."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                names.update(_names_in_target(target))
        elif isinstance(child, ast.AugAssign):
            names.update(_names_in_target(child.target))
        elif isinstance(child, ast.AnnAssign):
            names.update(_names_in_target(child.target))
        elif isinstance(child, (ast.For, ast.AsyncFor)):
            names.update(_names_in_target(child.target))
        elif isinstance(child, ast.comprehension):
            names.update(_names_in_target(child.target))
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            for item in child.items:
                if item.optional_vars is not None:
                    names.update(_names_in_target(item.optional_vars))
    return frozenset(names)


def _names_in_target(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out: set[str] = set()
        for elt in target.elts:
            out.update(_names_in_target(elt))
        return out
    if isinstance(target, ast.Starred):
        return _names_in_target(target.value)
    return set()


# ---------------------------------------------------------------------------
# Reference collection
# ---------------------------------------------------------------------------


def _collect_references(
    node: ast.AST,
    *,
    local_names: frozenset[str],
    sibling_methods: Optional[frozenset[str]] = None,
    skip_nested: bool = False,
) -> list[Reference]:
    seen: set[tuple[str, int]] = set()
    refs: list[Reference] = []

    def add(name: str, line: int) -> None:
        if not name:
            return
        root = name.split(".", 1)[0]
        if root in _BUILTINS or root in local_names:
            return
        key = (name, line)
        if key in seen:
            return
        seen.add(key)
        refs.append(Reference(name=name, line=line))

    def walk(scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(scope):
            if skip_nested and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                # For class-level collection we want methods' *signatures*
                # (decorators, defaults) but not their bodies.
                for dec in child.decorator_list:
                    walk(dec)
                for default in child.args.defaults + child.args.kw_defaults:
                    if default is not None:
                        walk(default)
                continue
            if isinstance(child, ast.Attribute):
                dotted = _attribute_chain(child)
                if dotted:
                    root = dotted.split(".", 1)[0]
                    if root == "self" and sibling_methods is not None:
                        parts = dotted.split(".")
                        if len(parts) >= 2 and parts[1] in sibling_methods:
                            add(parts[1], child.lineno)
                    else:
                        add(dotted, child.lineno)
                    continue  # do not descend further; the chain is captured
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                add(child.id, child.lineno)
            walk(child)

    walk(node)
    return refs


def _attribute_chain(node: ast.Attribute) -> str:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


# ---------------------------------------------------------------------------
# Source slicing + signatures
# ---------------------------------------------------------------------------


def _end_line(node: ast.AST, source_lines: list[str]) -> int:
    end = getattr(node, "end_lineno", None)
    if end is not None:
        return end
    return getattr(node, "lineno", 1)


def _slice_source(node: ast.AST, source_lines: list[str]) -> str:
    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", None)
    if end is None:
        return source_lines[start] if 0 <= start < len(source_lines) else ""
    snippet = "".join(source_lines[start:end])
    # Dedent common leading indentation so methods render cleanly as code blocks.
    return _dedent(snippet)


def _dedent(text: str) -> str:
    lines = text.splitlines(keepends=True)
    strippable = [line for line in lines if line.strip()]
    if not strippable:
        return text
    indent = min(len(line) - len(line.lstrip(" ")) for line in strippable)
    if indent == 0:
        return text
    return "".join(line[indent:] if line.strip() else line for line in lines)


def _function_signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def " if isinstance(fn, ast.AsyncFunctionDef) else "def "
    try:
        args = ast.unparse(fn.args)
    except Exception:  # pragma: no cover - defensive
        args = ""
    returns = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    return f"{prefix}{fn.name}({args}){returns}"


def _class_signature(cls: ast.ClassDef) -> str:
    bases = ", ".join(ast.unparse(b) for b in cls.bases)
    kwargs = ", ".join(f"{kw.arg}={ast.unparse(kw.value)}" for kw in cls.keywords if kw.arg)
    parts = [p for p in (bases, kwargs) if p]
    inside = f"({', '.join(parts)})" if parts else ""
    return f"class {cls.name}{inside}"


# Re-exported for testing convenience
__all__ = ["PythonAstParser"]
