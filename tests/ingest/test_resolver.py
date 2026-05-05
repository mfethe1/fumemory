from __future__ import annotations

import textwrap
from pathlib import Path

from memu.ingest.parsers.python_ast import PythonAstParser
from memu.ingest.resolve import LinkResolver, module_to_slug


def _parse_many(mods: dict[str, str]):
    parser = PythonAstParser()
    out = []
    for fqn, src in mods.items():
        parts = fqn.split(".")
        rel = Path(*parts[:-1], parts[-1] + ".py")
        out.append(parser.parse(textwrap.dedent(src), rel_path=rel, module_fqn=fqn))
    return out


def _links_for(resolver, files, fqn, qualname):
    file = next(f for f in files if f.module_fqn == fqn)
    return resolver.resolve_file(file).get(qualname, [])


def test_from_import_produces_link():
    files = _parse_many({
        "pkg.core": "class Foo:\n    def bar(self): return 1\n",
        "pkg.caller": "from pkg.core import Foo\n\ndef use():\n    return Foo()\n",
    })
    resolver = LinkResolver(files)
    links = _links_for(resolver, files, "pkg.caller", "use")
    assert {link.dst_slug for link in links} == {module_to_slug("pkg.core", "Foo")}


def test_aliased_import_produces_link_to_original_name():
    files = _parse_many({
        "pkg.core": "class Foo: pass\n",
        "pkg.caller": "from pkg.core import Foo as F\n\ndef make(): return F()\n",
    })
    resolver = LinkResolver(files)
    links = _links_for(resolver, files, "pkg.caller", "make")
    assert {link.dst_slug for link in links} == {module_to_slug("pkg.core", "Foo")}


def test_module_import_and_dotted_access():
    files = _parse_many({
        "pkg.util": "def helper(): return 1\n",
        "pkg.caller": "import pkg.util as u\n\ndef top():\n    return u.helper()\n",
    })
    resolver = LinkResolver(files)
    links = _links_for(resolver, files, "pkg.caller", "top")
    # Resolves to the specific symbol when we recognize it.
    assert {link.dst_slug for link in links} == {module_to_slug("pkg.util", "helper")}


def test_unknown_reference_is_not_linked():
    files = _parse_many({
        "pkg.core": "def visible(): return 1\n",
        "pkg.caller": "def top(): return never_heard_of_it()\n",
    })
    resolver = LinkResolver(files)
    assert _links_for(resolver, files, "pkg.caller", "top") == []


def test_same_module_reference_resolves():
    files = _parse_many({
        "pkg.only": "def helper(): return 1\n\ndef top(): return helper()\n",
    })
    resolver = LinkResolver(files)
    links = _links_for(resolver, files, "pkg.only", "top")
    assert [link.dst_slug for link in links] == [module_to_slug("pkg.only", "helper")]


def test_self_method_links_to_class_method():
    files = _parse_many({
        "pkg.only": (
            "class Greeter:\n"
            "    def greet(self):\n"
            "        return self.render()\n\n"
            "    def render(self):\n"
            "        return 'hi'\n"
        ),
    })
    resolver = LinkResolver(files)
    greet_links = _links_for(resolver, files, "pkg.only", "Greeter.greet")
    assert [link.dst_slug for link in greet_links] == [
        module_to_slug("pkg.only", "Greeter.render")
    ]


def test_self_link_to_owning_symbol_is_filtered():
    files = _parse_many({
        "pkg.only": "def recurse():\n    return recurse()\n",
    })
    resolver = LinkResolver(files)
    assert _links_for(resolver, files, "pkg.only", "recurse") == []
