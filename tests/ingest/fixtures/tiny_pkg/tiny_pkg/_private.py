"""Private helpers — the module is private (leading underscore)."""
from __future__ import annotations


def hidden_helper(name: str) -> str:
    return name.strip().lower()
