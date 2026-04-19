from __future__ import annotations

from memu.wiki.frontmatter import dump_frontmatter, parse_frontmatter


def test_parse_empty_text():
    fm, body = parse_frontmatter("")
    assert fm == {}
    assert body == ""


def test_parse_no_frontmatter():
    fm, body = parse_frontmatter("# hello\nworld\n")
    assert fm == {}
    assert body.startswith("# hello")


def test_roundtrip_simple():
    original = {"id": "abc123", "slug": "foo", "tags": ["a", "b"]}
    body = "# Foo\nBody.\n"
    raw = dump_frontmatter(original, body)
    fm, out_body = parse_frontmatter(raw)
    assert fm.get("id") == "abc123"
    assert fm.get("slug") == "foo"
    assert list(fm.get("tags") or []) == ["a", "b"]
    assert out_body.startswith("# Foo")


def test_parse_handles_missing_closing_delim():
    raw = "---\nslug: x\n\n# body only\n"
    fm, body = parse_frontmatter(raw)
    assert fm == {}
    assert "# body only" in body


def test_links_roundtrip():
    data = {
        "id": "n1",
        "slug": "foo",
        "links": [
            {"slug": "bar", "type": "related", "strength": 0.7},
            {"slug": "baz", "type": "extends", "strength": 0.5},
        ],
    }
    raw = dump_frontmatter(data, "body\n")
    fm, _ = parse_frontmatter(raw)
    assert fm.get("slug") == "foo"
    links = fm.get("links") or []
    assert len(links) == 2
    assert links[0]["slug"] == "bar"
    assert links[0]["type"] == "related"
