from __future__ import annotations

import textwrap
from pathlib import Path

from memu.ingest.parsers.python_ast import PythonAstParser


def _parse(source: str, module_fqn: str = "mod", rel: str = "mod.py"):
    parser = PythonAstParser()
    return parser.parse(
        textwrap.dedent(source),
        rel_path=Path(rel),
        module_fqn=module_fqn,
    )


def test_extracts_top_level_function_and_class():
    parsed = _parse(
        """
        \"\"\"Module docs.\"\"\"

        def foo(x):
            \"\"\"Return x.\"\"\"
            return x

        class Bar:
            \"\"\"A bar.\"\"\"

            def baz(self):
                return 1
        """
    )
    assert parsed.module_docstring == "Module docs."
    quals = {s.qualname for s in parsed.symbols}
    assert quals == {"foo", "Bar", "Bar.baz"}

    foo = next(s for s in parsed.symbols if s.qualname == "foo")
    assert foo.kind == "function"
    assert foo.docstring == "Return x."
    assert "return x" in foo.body_snippet

    bar = next(s for s in parsed.symbols if s.qualname == "Bar")
    assert bar.kind == "class"
    assert bar.signature.startswith("class Bar")


def test_async_function_signature():
    parsed = _parse(
        """
        async def ping(url: str) -> int:
            return 0
        """
    )
    sig = parsed.symbols[0].signature
    assert sig.startswith("async def ping(url: str)")
    assert "-> int" in sig


def test_absolute_from_import_and_alias():
    parsed = _parse(
        """
        from pkg.mod import Foo, Bar as B
        import sys
        """
    )
    modules = {(i.module, tuple(i.names)) for i in parsed.imports}
    assert ("pkg.mod", (("Foo", "Foo"), ("Bar", "B"))) in modules
    assert any(i.module == "sys" for i in parsed.imports)


def test_relative_import_is_resolved_against_package():
    parsed = _parse(
        """
        from . import utils
        from .sub import thing
        """,
        module_fqn="pkg.core",
    )
    modules = {i.module for i in parsed.imports}
    assert "pkg" in modules
    assert "pkg.sub" in modules


def test_self_method_references_resolve_to_sibling_methods():
    parsed = _parse(
        """
        class Greeter:
            def greet(self):
                return self._render()

            def _render(self):
                return 'hi'
        """
    )
    greet = next(s for s in parsed.symbols if s.qualname == "Greeter.greet")
    names = {ref.name for ref in greet.references}
    # Parser has already rewritten ``self._render`` into the bare method name.
    assert "_render" in names


def test_references_exclude_locals_and_builtins():
    parsed = _parse(
        """
        from foo import baz

        def do_thing(items):
            total = 0
            for item in items:
                total += len(item)
            return baz(total)
        """
    )
    fn = parsed.symbols[0]
    names = {ref.name for ref in fn.references}
    assert "baz" in names
    assert "len" not in names
    assert "total" not in names
    assert "item" not in names


def test_syntax_error_file_returns_empty_symbols_without_raising():
    parsed = _parse("def broken(:\n    pass\n")
    assert parsed.symbols == []
    assert parsed.module_docstring is None
