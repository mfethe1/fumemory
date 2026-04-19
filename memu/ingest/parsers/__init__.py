"""Language-specific source parsers.

Each parser implements :class:`LanguageParser` and yields a
:class:`ParsedFile` with enough information for the cross-file resolver
to emit wikilinks.

Adding a new language is a one-file change: implement :class:`LanguageParser`,
then register the class in :data:`PARSERS`. The Python parser is the only
one always available (stdlib ``ast``); other languages are behind optional
extras and register themselves lazily if imports succeed.
"""
from __future__ import annotations

from typing import Optional

from .base import (
    Import,
    LanguageParser,
    ParsedFile,
    ParsedSymbol,
    Reference,
)
from .python_ast import PythonAstParser


PARSERS: dict[str, LanguageParser] = {
    "python": PythonAstParser(),
}

_EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
}


def get_parser(language: str) -> Optional[LanguageParser]:
    return PARSERS.get(language)


def detect_language(path_suffix: str) -> Optional[str]:
    return _EXTENSION_MAP.get(path_suffix.lower())


__all__ = [
    "Import",
    "LanguageParser",
    "ParsedFile",
    "ParsedSymbol",
    "Reference",
    "PARSERS",
    "detect_language",
    "get_parser",
]
