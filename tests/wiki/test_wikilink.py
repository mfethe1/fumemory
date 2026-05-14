from __future__ import annotations

from memu.wiki.wikilink import parse_wikilinks


def test_simple_link():
    links = parse_wikilinks("see [[foo]] for details")
    assert len(links) == 1
    assert links[0].slug == "foo"
    assert links[0].alias is None
    assert links[0].anchor is None


def test_aliased_link():
    links = parse_wikilinks("refer to [[foo-parser|the parser]]")
    assert links[0].slug == "foo-parser"
    assert links[0].alias == "the parser"


def test_anchor_link():
    links = parse_wikilinks("[[design#goals]]")
    assert links[0].slug == "design"
    assert links[0].anchor == "goals"


def test_namespaced_slug():
    links = parse_wikilinks("cites [[paper/2312.12345]] and [[code/foo.py:FooParser]]")
    slugs = [link.slug for link in links]
    assert slugs == ["paper/2312.12345", "code/foo.py:FooParser"]
    assert links[0].namespace == "paper"


def test_dedup():
    links = parse_wikilinks("[[foo]] and [[foo]] again")
    assert len(links) == 1


def test_combined():
    links = parse_wikilinks("[[foo#bar|baz]]")
    assert links[0].slug == "foo"
    assert links[0].anchor == "bar"
    assert links[0].alias == "baz"
    assert links[0].display() == "baz"
