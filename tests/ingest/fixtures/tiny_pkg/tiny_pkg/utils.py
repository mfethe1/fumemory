"""Utility helpers reused by :mod:`tiny_pkg.core`."""
from __future__ import annotations


def format_hello(name: str) -> str:
    """Formats a friendly greeting for ``name``."""
    return f"hello, {name}"


def shout(text: str) -> str:
    return text.upper()
